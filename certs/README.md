Extra root CA certificates (PEM, `*.crt`) trusted inside the Docker image at build time
and at runtime — for networks that inspect HTTPS (corporate proxies, antivirus "web shield").
Leave this folder without .crt files when no inspection is in place.
Public certificates only; never put private keys here.
