# Deploying the API to AWS (free plan)

One EC2 `t3.small` (2 vCPU, 2 GB) runs the same Docker image as Render, behind
Caddy for HTTPS. Unlike Render's free tier it never sleeps, bursts to 20× the
CPU while an analysis runs (a `t3.small` earns enough burst credit for about
two dozen full-speed analyses an hour), and keeps accounts on a persistent
volume. The website stays on GitHub Pages.

**Cost: nothing, for up to six months.** AWS's free account plan gives $100
in credits (up to $200) and cannot be charged; this server uses roughly $20 of
credit a month. When the plan ends, AWS asks you to upgrade or the account
closes. Do not upgrade, and nothing is ever billed.

## What you need

| | Where | Cost |
|---|---|---|
| A DuckDNS name and token | duckdns.org, sign in with GitHub | free |
| An AWS account on the **Free plan** | aws.amazon.com | free |
| A Groq API key (optional, for written comments) | console.groq.com | free |

## 1. DuckDNS (2 minutes)

1. Go to **duckdns.org** and sign in with GitHub.
2. Type a name under *sub domain*, e.g. `chessvision-askretik`, and click **add domain**.
3. Copy the **token** shown at the top of the page.

## 2. AWS account (10 minutes)

1. Go to **aws.amazon.com → Create account**, and choose **Sign up for AWS**
   (the new experience) and **continue with GitHub**.
2. Choose the **Free plan** when asked.
3. In India, AWS must verify your identity (CERT-In rules): PAN card, voter ID
   or driving licence.
4. **If the sign-up asks for a card or bank details, stop.** The card-free
   sign-up is still being rolled out and may not be offered to every account.

## 3. Launch the server (5 minutes)

In the AWS console open **EC2 → Launch instance**:

| Setting | Value |
|---|---|
| Name | `chessvision` |
| Image (AMI) | **Ubuntu Server 24.04 LTS** (Free tier eligible) |
| Instance type | **t3.small** |
| Key pair | Proceed without a key pair |
| Network settings | tick **Allow SSH**, **Allow HTTPS** and **Allow HTTP** from the internet |
| Storage | **20 GiB gp3** |
| Advanced details → Credit specification | **Standard** |
| Advanced details → User data | the script below, with your three values |

```bash
#!/bin/bash
export DUCKDNS_DOMAIN=chessvision-askretik      # your DuckDNS name
export DUCKDNS_TOKEN=paste-your-duckdns-token
export LLM_API_KEY=paste-your-groq-key          # or leave empty
curl -fsSL https://raw.githubusercontent.com/ASKretik/chessvision/main/deploy/aws/bootstrap.sh | bash
```

Click **Launch instance**. The first build takes 10–15 minutes.

## 4. Check it

Open `https://<your-name>.duckdns.org/health`. It should show `"status":"ok"`.

If it doesn't after 20 minutes: select the instance, click **Connect →
EC2 Instance Connect → Connect**, and run

```bash
sudo tail -40 /var/log/chessvision-bootstrap.log
```

## 5. Point the website at it

```bash
gh variable set VITE_API_BASE -R ASKretik/chessvision --body "https://<your-name>.duckdns.org"
gh workflow run pages.yml -R ASKretik/chessvision
```

Then suspend the Render service in its dashboard so it stops rebuilding.

## How updates work

Every 5 minutes the server pulls `main` from GitHub (`update.sh`, run by a
systemd timer). If anything outside `frontend/`, `docs/`, `.github/` or the
Markdown files changed, it rebuilds and restarts the API. No AWS keys are
stored in GitHub. To deploy by hand from **Connect**:

```bash
sudo /opt/chessvision/deploy/aws/update.sh --force
```

| File | Purpose |
|---|---|
| `bootstrap.sh` | one-time setup: swap, Docker, clone, secrets, timer, first deploy |
| `update.sh` | refresh DuckDNS, pull, rebuild if the API changed |
| `compose.yml` | the API container plus Caddy |
| `Caddyfile` | HTTPS for the DuckDNS name |
| `.env` | secrets, written by `bootstrap.sh`, never committed |
