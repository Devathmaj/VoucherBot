<div align="center">

# 🎟️ VoucherBot

**An intelligent certification voucher aggregator**

*Continuously monitors community and official sources for certification discounts, free exam opportunities, beta exams, and promotional campaigns.*

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-see%20LICENSE-lightgrey)
![Deploy](https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=white)
![Status](https://img.shields.io/badge/status-active-success)

</div>

---

## Big News — No More Setup Required!

We heard your request loud and clear. 🎉

Instead of going through the entire setup and hosting it yourself, you can now **add VoucherBot to your Discord or Telegram** and it will alert you the moment a certification voucher shows up. No code, no deploy, no hassle.

Just head over to **[voucherbot-preview.pages.dev/#notifications](https://voucherbot-preview.pages.dev/#notifications)** to learn all about it and get it set up in minutes.

---

## Table of Contents

- [Overview](#overview)
- [What You'll Need](#what-youll-need)
- [Setup — Step by Step](#setup--step-by-step)
- [Account & API Key Setup Guides](#account--api-key-setup-guides)
- [Additional Documentation](#additional-documentation)
- [Contributing](#contributing)
- [License](#license)
- [Goals](#goals)
- [Live Preview](#live-preview)

---

## Overview

VoucherBot is a program that automatically hunts for certification exam discounts across the internet — so you don't have to. Instead of manually checking dozens of websites, blogs, forums, and communities yourself, VoucherBot does it all in the background, filters out irrelevant results, and alerts you whenever it finds a legitimate certification promotion.

**The easiest way to use VoucherBot** is to add it directly to your **Discord or Telegram** — get notified the instant a voucher drops, no setup at all. See the [Notifications](https://voucherbot-preview.pages.dev/#notifications) page to get started.

Prefer to run it yourself? The whole thing runs in the cloud (on a free service called Render), so once it's set up, it runs 24/7 without your computer needing to be on. That path is covered below.

---

## What You'll Need

Before you begin, you'll need to create **free accounts** on five websites. Don't worry — each one has a step-by-step guide below, and none of them require any payment information to get started.

| Service | What it does |
|---------|--------------|
| **Groq** | The AI brain of VoucherBot — reads posts it finds and decides if they contain real voucher offers |
| **Supabase** | The database — stores everything VoucherBot discovers so it doesn't notify you about the same thing twice |
| **Resend** | The email sender — delivers voucher notifications to your inbox |
| **Google Gemini** | A backup AI brain — automatically takes over if Groq has any issues |
| **Reddit** | Where VoucherBot looks for voucher posts — you need to give it permission to read Reddit |

You'll also need a free account on **Render**, which is the service that actually runs VoucherBot in the cloud (covered in the deployment guides below).

> 💡 **Tip:** Open each setup guide in its own browser tab before you start — it's faster to have them all ready.

---

## Setup — Step by Step

### Step 1 — Make a copy of VoucherBot

Go to the [VoucherBot GitHub page](https://github.com) and click the **Fork** button in the top-right corner. This makes your own personal copy of VoucherBot that you can connect to Render.

> **What's "forking"?** Think of it like making a photocopy of a recipe. You get your own copy to work with, and any updates to the original can be pulled into yours later. You don't need to understand the code inside — you just need your own copy so Render can access it.

If you'd rather not create a GitHub account, you can also download the source code directly as a ZIP file — but GitHub is strongly recommended because it makes future updates much easier.

---

### Step 2 — Download the settings file

Find the file called `.env.example` in the repository and download it to your computer. Rename it to `.env` (just remove the word "example" from the name).

This file is like a form with blank fields — you'll fill in the values you collect from each setup guide. You do **not** need to open a terminal or write any code to do this. A plain text editor (like Notepad on Windows or TextEdit on Mac) works perfectly.

---

### Step 3 — Complete the account setup guides

Work through **all five** of the guides in the [Account & API Key Setup Guides](#account--api-key-setup-guides) section below. Each guide gives you a key (a long string of letters and numbers) that you paste into your `.env` file.

Once all five guides are done, your `.env` file should have every blank filled in.

---

### Step 4 — Save your settings somewhere safe

Once your `.env` file is complete, keep it somewhere you won't lose it — either saved on your computer or copied into a notes app. You'll need to paste these values into Render during the next step.

---

### Step 5 — Deploy to Render (put it on the internet)

Follow the [Render Deployment Guide](./docs/setup/render-deployment.md) to connect your GitHub copy of VoucherBot to Render, enter your settings, and launch the app.

Once this step is done, VoucherBot is live and running in the cloud. You'll start receiving email notifications whenever it finds certification vouchers — no further action needed.

---

### Optional — Keep it from going to sleep

Render's free plan pauses your app if nobody visits it for a while. To prevent this, follow the optional [UptimeRobot Setup Guide](./docs/setup/uptime-bot-setup.md) — it sets up a simple automated "ping" that keeps VoucherBot awake around the clock.

---

## ⚠️ One Important Note About Shutting Down

If you ever need to stop or restart VoucherBot (for example, from the Render dashboard), **don't force-stop it while it's in the middle of checking a source**. 

VoucherBot uses a locking system to make sure it doesn't process the same source twice at the same time. If you cut the power abruptly (like clicking "Restart" right as it's working), that lock can get stuck — which may cause errors the next time it starts up.

**The safe way:** Use a single gentle stop (one Ctrl+C, or a graceful restart from Render's dashboard) and wait a moment. VoucherBot will finish what it's doing and then stop cleanly.

If you do see errors mentioning "lock" or "connection" after a restart, don't panic — simply restarting the app one more time clears them automatically.

---

## Account & API Key Setup Guides

Complete **all five** of these guides before attempting to deploy. Each one walks you through creating a free account, finding the key or credential you need, and where to paste it in your `.env` file.

| Service | What you're setting up | Guide |
|---------|------------------------|-------|
| **Groq** | Primary AI — analyses posts to find real voucher offers | [Groq Setup](./docs/setup/groq-setup.md) |
| **Supabase** | Database — remembers what VoucherBot has already found | [Supabase Setup](./docs/setup/supabase-setup.md) |
| **Resend** | Email sender — delivers your voucher notifications | [Resend Setup](./docs/setup/resend-setup.md) |
| **Google Gemini** | Backup AI — takes over automatically if Groq has issues | [Gemini Setup](./docs/setup/gemini-setup.md) |
| **Reddit API** | Gives VoucherBot permission to read Reddit posts | [Reddit Setup](./docs/setup/reddit-setup.md) |

### Deployment Guides

Once the five account guides above are done, use these to get VoucherBot live:

| Guide | What it covers |
|-------|----------------|
| [Render Deployment](./docs/setup/render-deployment.md) | Connects your copy of VoucherBot to Render and launches it in the cloud |
| [UptimeRobot Setup](./docs/setup/uptime-bot-setup.md) *(optional)* | Keeps VoucherBot from going to sleep on Render's free plan |

---

## Additional Documentation

These are for users who want to dig deeper into how VoucherBot works under the hood. **You don't need to read any of these to get set up.**

| Resource | Description |
|----------|-------------|
| [Architecture](./docs/details/architecture.md) | A detailed overview of the system architecture, application components, data flow, and design decisions. |
| [Detailed Summary](./docs/details/detailed-summary.md) | A highly detailed summary of the entire project. |
| [Configuration](./docs/details/configuration.md) | A full reference of all available configuration options. |
| [Schema](./docs/details/schema.md) | The project's data schema. |
| [Project Info](./docs/details/project-info.md) | General information about the project. |
| [Testing](./docs/details/testing.md) | How to run the automated test suite, what to expect while testing, and simple troubleshooting steps. |
| [Sources](./sources/source.md) | An overview of all the sources VoucherBot monitors. |

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution guidelines, development workflow, and best practices before submitting a pull request.

---

## License

This project is licensed under the **Apache License 2.0**. See [LICENSE](./LICENSE) for the full terms.

---

## Goals

- **Reduce the time** spent searching for certification discounts.
- **Aggregate information** from multiple trusted sources.
- **Minimize false positives** using AI-assisted filtering.
- **Provide timely notifications** for new certification opportunities.
- **Offer an extensible platform** that supports additional providers with minimal development effort.

---

## Live Preview

Curious what VoucherBot actually finds? A selection of vouchers and certification offers collected by the bot is publicly displayed at:

**[voucherbot-preview.pages.dev](https://voucherbot-preview.pages.dev/)**

Feel free to browse through it and see the kind of deals VoucherBot surfaces — no setup required.

![VoucherBot Preview](docs/images/voucherbot-preview.png)