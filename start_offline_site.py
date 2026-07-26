import http.server
import socketserver
import webbrowser

PORT = 8080
DIRECTORY = "."

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

print(f"==================================================")
print(f"   NamSPI Offline Web Server")
print(f"   Opening site at http://localhost:{PORT}")
print(f"   Press Ctrl+C to stop the server.")
print(f"==================================================")

webbrowser.open(f"http://localhost:{PORT}")

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
