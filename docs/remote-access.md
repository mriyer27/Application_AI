# Secure phone access

The dashboard runs on your laptop and contains your job search: matches,
applications, legal declarations, emails. It must never be reachable from the
public internet. The supported way to use it from your phone is a **private VPN
between your own devices**. Tailscale is recommended; plain WireGuard works too.

Before you start: set the login (`job-agent dashboard set-password`) and check
that `job-agent dashboard` works at <http://127.0.0.1:8765> on the laptop.

## Recommended: Tailscale + `tailscale serve` (HTTPS, dashboard stays on 127.0.0.1)

1. **Install Tailscale on the laptop and the phone** (<https://tailscale.com/download>,
   App Store / Google Play) and sign in to the **same** account on both. Both
   devices should appear at <https://login.tailscale.com/admin/machines>.
2. In the Tailscale admin console, under **DNS**, enable **MagicDNS** and
   **HTTPS certificates**.
3. Keep the dashboard on loopback (`config/config.yaml`):

   ```yaml
   dashboard:
     host: 127.0.0.1
     port: 8765
     secure_cookies: true            # the phone talks HTTPS to tailscale serve
     allowed_hosts: ["laptop.your-tailnet.ts.net"]   # your MagicDNS name
   ```

4. Publish it **inside your tailnet only**:

   ```bash
   tailscale serve --bg 8765
   tailscale serve status           # shows https://laptop.your-tailnet.ts.net
   ```

   `tailscale serve` terminates HTTPS with a certificate for your MagicDNS name
   and forwards to `127.0.0.1:8765`. Only devices logged in to your tailnet can
   reach it. Put exactly the name it shows into `dashboard.allowed_hosts`.
5. Restart `job-agent dashboard`, turn on Tailscale on the phone, open the
   `https://…ts.net` URL, sign in, and optionally add it to the home screen.

Notes:

* If the phone shows **"Bad Request: unexpected Host header"**, the name you
  typed is not in `dashboard.allowed_hosts` (DNS-rebinding protection). Add the
  exact MagicDNS name and restart the dashboard.
* With `secure_cookies: true` the session cookie is only sent over HTTPS. Use
  the `https://…ts.net` URL on the laptop as well; most browsers also accept it
  on `http://127.0.0.1`, but if you cannot stay signed in there, that is why.
* Behind `tailscale serve` every request reaches the dashboard from
  `127.0.0.1`, so the login rate limit is shared by all your devices.
* To stop publishing: `tailscale serve --https=443 off` (or `tailscale serve reset`).

## Alternative: bind to the Tailscale IP (HTTP inside the tailnet)

If you cannot use `tailscale serve`, the dashboard can listen on the laptop's
Tailscale address (`tailscale ip -4`, a `100.x.y.z` address):

```yaml
dashboard:
  host: 100.101.102.103            # your laptop's Tailscale IPv4 address
  allow_non_localhost: true         # explicit opt-in; required for any non-loopback host
  secure_cookies: false             # plain HTTP (WireGuard-encrypted by Tailscale)
```

Open `http://100.101.102.103:8765` on the phone. `job-agent dashboard` prints a
loud warning for any non-loopback address. Traffic is encrypted by Tailscale's
WireGuard tunnel, but the browser sees plain HTTP. Tailscale IPs can change if
you remove and re-add the device; update the config if that happens. Consider a
Tailscale ACL that allows only your phone to reach port 8765 on the laptop.

## Alternative: WireGuard

With your own WireGuard setup, bind the dashboard to the laptop's WireGuard
interface address (for example `10.8.0.1`) with `allow_non_localhost: true`,
exactly as in the Tailscale-IP option, and make sure the laptop firewall only
accepts port 8765 on that interface. Never bind it to a LAN or public address.

## Never do this

* **Never bind to `0.0.0.0` / `::`** (all interfaces), and never bind to your
  Wi-Fi/LAN address, especially on public or shared Wi-Fi (cafés, campus,
  hotels). `job-agent dashboard` refuses wildcard binds.
* **Never port-forward** the dashboard on your router.
* **Do not use public tunnels**: ngrok, Cloudflare quick tunnels, localtunnel,
  or **Tailscale Funnel** (`tailscale funnel`) publish the dashboard to the whole
  internet, where it becomes a target for password guessing. The login is a
  second line of defence, not the first.
* Do not share the dashboard password or your Tailscale account.

## When the laptop sleeps

Everything runs on the laptop. When it sleeps, hibernates, loses power or is
offline, the dashboard is unreachable from the phone (the page will time out),
and scheduled tasks do not run either. When it wakes, the dashboard and daemon
continue if they are still running; missed scheduled runs are handled
according to `scheduler.misfire_grace_minutes`. If you want phone access during the day, keep the
laptop plugged in with sleep disabled while on power, and start
`job-agent dashboard` (and `job-agent daemon`) at login (see `scheduling.md`).
Restarting the dashboard signs you out.

## Checklist

- [ ] `dashboard.host` is `127.0.0.1` (with `tailscale serve`) or a private VPN address
- [ ] `allow_non_localhost` is `true` only for a VPN address
- [ ] `secure_cookies: true` when using `tailscale serve`
- [ ] `allowed_hosts` contains only your own MagicDNS name
- [ ] no `tailscale funnel`, ngrok, port-forwarding or `0.0.0.0`
- [ ] password of at least 12 characters set with `job-agent dashboard set-password`
