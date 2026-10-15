# Running Pickup Poll on an always-on server

Tested pieces: the app, its proxy option and the tests. **Not tested end to end:** the setup script and the Caddy
config have not been run on a real VM yet, so check each step's output.

## What you get
The app runs as a service that restarts by itself and starts on boot. Caddy sits in front of it, gets an HTTPS
certificate automatically, and passes the visitor's address along so the PIN lockout and rate limits work per person.
The database is a single file in `/var/lib/pickup-poll`, copied daily to `/var/backups/pickup-poll` (14 days kept).

## Azure steps
1. Portal, Create a resource, **Virtual machine**. Ubuntu Server 24.04 LTS, size **B1s** (or the cheapest B-series),
   authentication **SSH public key**, inbound ports **SSH, HTTP, HTTPS**.
2. When it exists, open its **Public IP address** resource and set a **DNS name label** (for example `fnsc-poll`).
   Your host name is then `fnsc-poll.<region>.cloudapp.azure.com`. Also set the IP to **Static**.
3. In Cost Management, set a **budget alert** so credits cannot run out unnoticed.
4. Connect and install:
   ```bash
   ssh azureuser@<host name>
   sudo apt-get install -y git
   git clone https://github.com/elmir132/pickup-poll.git && cd pickup-poll
   sudo ./deploy/setup.sh <host name>
   ```
   It prints the organizer PIN once. Write it down (it is also in `/etc/pickup-poll.env`, readable by root only).
5. Open `https://<host name>/` on your phone.

## Updating later
```bash
cd pickup-poll && git pull && sudo ./deploy/setup.sh <host name> && sudo systemctl restart pickup-poll
```
The PIN and the data are kept.

## If something is wrong
`sudo systemctl status pickup-poll caddy` and `sudo journalctl -u pickup-poll -u caddy -n 50`.
