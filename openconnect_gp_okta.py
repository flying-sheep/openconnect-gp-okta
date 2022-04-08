#!/usr/bin/env python3
"""
OpenConnect wrapper which logs into a GlobalProtect gateway,
authenticating with Okta.
"""
from __future__ import annotations

import base64
import contextlib
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import urllib.parse
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import keyring
import lxml.etree
import requests

try:
    import pyotp
except ImportError:
    pyotp = None


__version__ = '1.0'


def check(r: requests.Response) -> requests.Response:
    r.raise_for_status()
    return r


def extract_form(html: bytes) -> tuple[str, dict[str, str]]:
    form = lxml.etree.fromstring(html, lxml.etree.HTMLParser()).find('.//form')
    return (
        form.attrib['action'],
        {inp.attrib['name']: inp.attrib['value'] for inp in form.findall('input')},
    )


def prelogin(s: requests.Session, gateway: str) -> str:
    r = check(s.post(f'https://{gateway}/ssl-vpn/prelogin.esp'))
    saml_req_html = base64.b64decode(
        lxml.etree.fromstring(r.content).find('saml-request').text
    )
    saml_req_url, saml_req_data = extract_form(saml_req_html)
    assert 'SAMLRequest' in saml_req_data
    return f'{saml_req_url}?{urllib.parse.urlencode(saml_req_data)}'


def post_json(s: requests.Session, url: str, data: Any) -> Any:
    # https://developer.okta.com/docs/reference/api/authn/#context-object
    data = {**data, 'context': {'deviceToken': str(uuid.getnode())}}
    r = check(
        s.post(url, data=json.dumps(data), headers={'Content-Type': 'application/json'})
    )
    return r.json()


def okta_auth(
    s: requests.Session,
    domain: str,
    username: str,
    password: str,
    totp_key: str | None,
) -> str:
    r = post_json(
        s,
        f'https://{domain}/api/v1/authn',
        {'username': username, 'password': password},
    )

    if r['status'] == 'MFA_REQUIRED':

        def priority(factor: dict[str, Any]):
            return {'token:software:totp': 2 if totp_key is None else 0, 'push': 1}.get(
                factor['factorType'], 2
            )

        for factor in sorted(r['_embedded']['factors'], key=priority):
            if factor['factorType'] == 'push':
                # https://developer.okta.com/docs/reference/api/authn/#verify-push-factor
                url = factor['_links']['verify']['href']
                while True:
                    r = post_json(
                        s, url, {'stateToken': r['stateToken'], 'rememberDevice': True}
                    )
                    if r['status'] != 'MFA_CHALLENGE':
                        break
                    assert r['factorResult'] == 'WAITING'
                break
            if factor['factorType'] == 'sms':
                url = factor['_links']['verify']['href']
                r = post_json(s, url, {'stateToken': r['stateToken']})
                assert r['status'] == 'MFA_CHALLENGE'
                code = input('SMS code: ')
                r = post_json(s, url, {'stateToken': r['stateToken'], 'passCode': code})
                break
            if re.match('token(?::|$)', factor['factorType']):
                url = factor['_links']['verify']['href']
                r = post_json(s, url, {'stateToken': r['stateToken']})
                assert r['status'] == 'MFA_CHALLENGE'
                if (factor['factorType'] == 'token:software:totp') and (
                    totp_key is not None
                ):
                    code = pyotp.TOTP(totp_key).now()
                else:
                    code = input(
                        f'One-time code for {factor["provider"]} '
                        f'({factor["vendorName"]}): '
                    )
                r = post_json(s, url, {'stateToken': r['stateToken'], 'passCode': code})
                break
        else:
            raise Exception('No supported authentication factors')

    assert r['status'] == 'SUCCESS'
    return r['sessionToken']


def okta_saml(
    s: requests.Session,
    saml_req_url: str,
    username: str,
    password: str,
    totp_key: str | None,
) -> tuple[str, dict[str, str]]:
    domain = urllib.parse.urlparse(saml_req_url).netloc

    # Just to set DT cookie
    check(s.get(saml_req_url))

    token = okta_auth(s, domain, username, password, totp_key)

    r = check(
        s.get(
            f'https://{domain}/login/sessionCookieRedirect',
            params={'token': token, 'redirectUrl': saml_req_url},
        )
    )
    saml_resp_url, saml_resp_data = extract_form(r.content)
    assert 'SAMLResponse' in saml_resp_data
    return saml_resp_url, saml_resp_data


def complete_saml(
    s: requests.Session, saml_resp_url: str, saml_resp_data: dict[str, str]
) -> tuple[str, str]:
    r = check(s.post(saml_resp_url, data=saml_resp_data))
    return r.headers['saml-username'], r.headers['prelogin-cookie']


@contextlib.contextmanager
def signal_mask(how: int, mask: set[signal.Signals]) -> set[signal.Signals]:
    old_mask = signal.pthread_sigmask(how, mask)
    try:
        yield old_mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


@contextlib.contextmanager
def signal_handler(
    num: signal.Signals, handler: Callable
) -> AbstractContextManager[Callable]:
    old_handler = signal.signal(num, handler)
    try:
        yield old_handler
    finally:
        signal.signal(num, old_handler)


@contextlib.contextmanager
def popen_forward_sigterm(
    args: list[str], *, stdin=None
) -> AbstractContextManager[subprocess.Popen]:
    with signal_mask(signal.SIG_BLOCK, {signal.SIGTERM}) as old_mask:
        with subprocess.Popen(
            args,
            stdin=stdin,
            preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, old_mask),
        ) as p:
            with signal_handler(signal.SIGTERM, lambda *args: p.terminate()):
                with signal_mask(signal.SIG_SETMASK, old_mask):
                    yield p
                    if p.stdin:
                        p.stdin.close()
                    os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)


def main(
    gateway: str,
    username: str | None = None,
    password: str | None = None,
    *,
    totp_key: str | None = None,
    sudo: bool = False,
) -> None:
    if (totp_key is not None) and (pyotp is None):
        print('--totp-key requires pyotp!', file=sys.stderr)
        sys.exit(1)

    if username is None:
        username = input('Username: ')
    if password is None:
        password = keyring.get_password(gateway, username)
        if password is None:
            password = getpass.getpass()

    with requests.Session() as s:
        saml_req_url = prelogin(s, gateway)
        saml_resp_url, saml_resp_data = okta_saml(
            s, saml_req_url, username, password, totp_key
        )
        saml_username, prelogin_cookie = complete_saml(s, saml_resp_url, saml_resp_data)

    subprocess_args = [
        'openconnect',
        gateway,
        '--protocol=gp',
        f'--user={saml_username}',
        '--usergroup=gateway:prelogin-cookie',
        '--passwd-on-stdin',
    ]

    if sudo:
        subprocess_args = ['sudo', *subprocess_args]

    with popen_forward_sigterm(subprocess_args, stdin=subprocess.PIPE) as p:
        p.stdin.write(prelogin_cookie.encode())
    sys.exit(p.returncode)


def cli():
    import typer

    typer.run(main)


if __name__ == '__main__':
    cli()
