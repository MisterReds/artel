"""HTTP servers for loopback use. The stdlib's HTTPServer.server_bind() calls socket.getfqdn(host), a reverse DNS
lookup that takes ~35 s on macOS CI runners (and on any machine with a broken resolver) for every server started."""
import socketserver
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # without the getfqdn() of http.server.HTTPServer.server_bind
        self.server_name, self.server_port = self.server_address[:2]
