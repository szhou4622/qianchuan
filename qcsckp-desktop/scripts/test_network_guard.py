"""Unit tests may use loopback fixtures, never a live advertising service."""
import socket


def install():
    original_connect = socket.socket.connect
    original_create = socket.create_connection

    def allowed(address):
        if isinstance(address, tuple):
            host = str(address[0]).lower()
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise AssertionError("Live network is forbidden in isolated tests")

    def connect(sock, address):
        allowed(address)
        return original_connect(sock, address)

    def create(address, *args, **kwargs):
        allowed(address)
        return original_create(address, *args, **kwargs)

    socket.socket.connect = connect
    socket.create_connection = create
