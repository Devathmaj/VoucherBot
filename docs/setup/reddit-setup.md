# Reddit API Access & OAuth Credentials

## ⚠️ Important: Reddit API Access is Now Approval-Based

As of recent policy changes, Reddit no longer allows open API access. **All API requests must now be submitted for review and approved by Reddit before you can obtain credentials.** This process takes time — potentially days or even weeks — and **your request may be rejected** with no guarantee of approval. Plan accordingly before building anything that depends on the Reddit API.

---

## Requesting API Access

Depending on your use case, submit a request through the appropriate form below:

### Personal Use
If you're building something for personal/non-commercial use, submit your request here:

👉 [Reddit API Request — Personal](https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164)

![Personal API Request Form](../images/reddit_personal.png)

### Commercial Use
If you're building a commercial product or enterprise application, use this form instead:

👉 [Reddit API Request — Commercial/Enterprise](https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164&tf_42139884615700=api_request_type_enterprise_clone)

![Commercial API Request Form](../images/reddit_commercial.png)

---

## After Approval

Once Reddit approves your request, you should receive a **Client ID** and **Client Secret**. Add these to your `.env` file as follows:

```dotenv
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
```

---

## ⏳ Still Waiting for Approval?

Since Reddit API approval can take days or even weeks, you don't have to wait for it before deploying the project. Keep Reddit ingestion disabled in your `.env` file in the meantime:

```dotenv
REDDIT_INGESTION_ENABLED=false
```

With `REDDIT_INGESTION_ENABLED=false` the Reddit OAuth API is never called, but the Reddit sources still run and collect posts through Reddit's public RSS feeds, so you get coverage without waiting for approval.

Once your request is approved, add your credentials and set the flag back to `true` to switch collection to the OAuth API.

---

## ⚠️ A Note from the Author

At the time of writing, I have not yet received my own API credentials from Reddit, so unfortunately I'm unable to provide further step-by-step instructions beyond this point. Here's hoping your request gets approved — good luck! 🤞