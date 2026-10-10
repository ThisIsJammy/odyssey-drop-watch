# Jailer 2 Pune watcher — why it cannot run on GitHub Actions

`jailer.yml` is **disabled**: the watcher logic works, but neither data source
is reachable from a GitHub runner.

Probed 2026-10-10 from a runner in Azure `eastus2` (IP 48.214.54.50, US):

| host | result | edge |
|---|---|---|
| `www.district.in` | **403 Access Denied** | Akamai (`AKA_A2` cookie, `ak_p` timing) |
| `api3.pvrcinemas.com` | **403 Access Denied** | `server: AkamaiGHost` |
| `www.zomato.com` | 200 OK | envoy / AWS — no block |

Both blocked hosts are behind Akamai and refuse foreign datacenter IPs. The
same requests, same headers, succeed from the user's residential IP in India.
Adding headers does not help — it is an edge IP policy, not a bot challenge
that a browser-shaped request satisfies. Getting past it would require
proxying through a residential/India exit, which is the same arms race
declined for Apple Cinemas (see `apple_cinemas_NOT_TRACKABLE.py`), so it
stops here.

This is also why the Zomato/Burger King watcher *can* run in the cloud while
this one cannot — do not assume a new Indian site is runner-reachable; probe
it first.

**What runs instead:** `~/jailer_watch/` on the user's Mac via launchd, which
polls from the residential IP. Tradeoff: only while the Mac is awake and
online.

**Best channel of all is first-party:** BookMyShow's own "Notify Me" on the
Jailer 2 page, which needs no scraping and fires even with the Mac asleep.
