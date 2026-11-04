Extra root CA certificates (PEM, `*.crt` — `*.pem` / `*.cer` exports are accepted too) trusted inside
the Docker image at build time and at runtime — for networks that inspect HTTPS (corporate proxies
such as Zscaler, antivirus "web shield").
Leave this folder without certificates when no inspection is in place.
Public certificates only; never put private keys here.

`zscaler.crt` is the corporate proxy's root CA (Zscaler Root CA): copy it from the download folder
(`D:\Downloads\zscaler.cer` → `certs/zscaler.crt`) when building on a Zscaler-inspected network.
Remove it before building a production bundle if your policy requires it.
