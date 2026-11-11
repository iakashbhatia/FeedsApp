# Feed notifier

Sends a push notification to your phone when an RSS or Atom feed gets a new item.
It runs on GitHub Actions four times a day and delivers through the free
[ntfy](https://ntfy.sh) app, so there is no server to run.

Out of the box it watches:

- **AWS News Blog**: `https://aws.amazon.com/blogs/aws/feed/`
- **AWS What's New**: `https://aws.amazon.com/about-aws/whats-new/recent/feed/`

## Set up

1. **Get the ntfy app** (App Store or Google Play). Tap **+** and subscribe to a
   topic name that is long and hard to guess, for example `feeds-` followed by 20
   random characters. Anyone who knows the topic name can read or send to it, so
   treat it like a password.
2. **Put these files in a GitHub repo** (private is fine), on the default branch.
3. **Add the topic as a secret.** In the repo: Settings > Secrets and variables >
   Actions > New repository secret. Name it `NTFY_TOPIC` and paste the topic name.
4. **Run it once.** Actions tab > Check feeds > Run workflow. Within a minute you
   should get one "Now watching ..." notification per feed.

From then on it checks on its own at about 7 am, 11 am, 3 pm and 7 pm Pacific
(an hour later during daylight saving time). GitHub can start scheduled runs a
few minutes late.

## What to expect

- **First run** only confirms each feed is being watched. It does not send the
  backlog of existing items.
- **New items** arrive one notification each, oldest first. Tapping one opens the post.
- **Busy checks** are rolled up. If a feed has more new items than its
  `max_individual` setting, you get a single notification listing the titles
  instead. Tapping it opens the feed's site.
- **Failures are visible.** If a feed cannot be read or a notification cannot be
  sent, the run goes red in the Actions tab with the reason, and anything that was
  not delivered is retried on the next run. Items already delivered are not sent again.

## Change what it watches

Edit `feeds.json`. Each feed takes:

| Field | Meaning |
| --- | --- |
| `url` | The RSS or Atom feed address (required) |
| `name` | Label shown in notifications |
| `site` | Page to open when a summary notification is tapped |
| `max_individual` | Most new items to send one by one in a single check. More than this arrive as one summary. `0` means always summarise. Default 10 |

A feed you add starts with its own "Now watching" confirmation on the next run.

To change the times, edit the `cron` line in `.github/workflows/check-feeds.yml`.
Times there are in UTC.

## Optional settings

- `NTFY_TOKEN` (secret): an ntfy access token, if you use a protected topic.
- `NTFY_SERVER` (repository variable): your own ntfy server instead of `https://ntfy.sh`.

## How it works

`check_feeds.py` (Python standard library only) downloads each feed, compares the
item ids with `state/seen.json`, sends what is new, and updates that file. The
workflow commits `state/seen.json` back to the repo after each run, which is how
it remembers between runs.

Each run takes well under a minute. Public repos run Actions for free; a private
repo uses about 120 of your plan's included Actions minutes a month.

## Try it locally

```sh
python3 check_feeds.py --dry-run          # print what would be sent, change nothing
python3 -m unittest discover -s tests -v  # run the tests
```
