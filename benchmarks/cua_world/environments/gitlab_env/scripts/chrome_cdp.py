#!/usr/bin/env python3
"""Authenticate the visible Chrome tab and open the deterministic start page."""

import json
import sys
import time
from urllib.request import urlopen

import websocket


DEBUG_BASE = "http://127.0.0.1:9222"


def targets():
    with urlopen(f"{DEBUG_BASE}/json", timeout=3) as response:
        return json.load(response)


def connect_page():
    pages = [item for item in targets() if item.get("type") == "page"]
    if not pages:
        raise RuntimeError("Chrome has no debuggable page target")
    return websocket.create_connection(
        pages[0]["webSocketDebuggerUrl"],
        timeout=10,
        origin=DEBUG_BASE,
    )


class Cdp:
    def __init__(self, socket):
        self.socket = socket
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        call_id = self.next_id
        self.socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")


def wait_for(cdp, expression, expected=True, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cdp.evaluate(expression) == expected:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for browser expression: {expression}")


def main():
    destination = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8929/dashboard/projects?sort=name_asc"
    socket = connect_page()
    cdp = Cdp(socket)
    wait_for(cdp, "document.readyState", "complete")

    login_script = r"""
(() => {
  const login = document.querySelector('#user_login, input[name="user[login]"]');
  const password = document.querySelector('#user_password, input[name="user[password]"]');
  const submit = document.querySelector('input[type="submit"], button[type="submit"]');
  if (!login || !password || !submit) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(login, 'byteblaze');
  login.dispatchEvent(new Event('input', {bubbles: true}));
  setter.call(password, 'V9xQ4mL2pR7s!');
  password.dispatchEvent(new Event('input', {bubbles: true}));
  submit.click();
  return true;
})()
"""
    if not cdp.evaluate(login_script):
        current = cdp.evaluate("location.href")
        if "/users/sign_in" in str(current):
            raise RuntimeError("GitLab sign-in controls were not found")

    deadline = time.time() + 90
    while time.time() < deadline:
        current = str(cdp.evaluate("location.href") or "")
        if "/users/sign_in" not in current:
            break
        time.sleep(1)
    else:
        raise RuntimeError("GitLab sign-in did not complete")

    cdp.call("Page.navigate", {"url": destination})
    wait_for(cdp, "document.readyState", "complete")
    wait_for(cdp, "document.body && document.body.innerText.includes('Projects')", True)

    # GitLab can surface a one-time "You pushed to master" banner immediately
    # after the stable-world seed push. It is unrelated to the episode and
    # shifts the project list away from the video start state, so dismiss the
    # real banner control if it appears during page hydration.
    for _ in range(8):
        cdp.evaluate(
            """
(() => {
  const button = document.querySelector(
    'button.js-close-banner[aria-label="Dismiss"]'
  );
  if (!button) return false;
  button.click();
  return true;
})()
"""
        )
        time.sleep(0.25)

    socket.close()
    print(f"Chrome ready at {destination}")


if __name__ == "__main__":
    main()
