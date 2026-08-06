# Email — internal update to the owners

Draft below. Subject line options first, then the body. Slide deck to attach:
`docs/3PL-Portal-Overview.pptx`.

---

**Subject:** 3PL Portal — a quick update on something I've been building

**Alternatives:** *"The 3PL service now has a customer portal (and bills itself)"* ·
*"Update: 3PL customer portal + automated billing, now live"*

---

Hi both,

Now that the 3PL service is running properly with Mova, I wanted to show you something
I've been building alongside it that you haven't seen yet.

**The short version:** we now have a web portal where a 3PL customer can log in and see
their own stock in real time, and which calculates our weekly charges to them
automatically. It's live, running off our NetSuite data, and Mova's real numbers are in it.

**Why it exists.** Storing and handling stock we don't own created two jobs nobody had a
system for. The first is answering "how much of our stock do you have?" — which was
heading towards a weekly spreadsheet someone puts together by hand. The second is billing:
every week we have to work out how many containers were unloaded, how many units were put
away, how many pallets we stored and how many units we picked, then price all of it. That
was five saved searches, run manually, with no safety net.

**What it does now**

- **The customer sees their own stock, live.** Six views — stock on order, what's been
  received, what's on hand, what's been dispatched, their invoices, and the agreed rate
  card. Stock on hand refreshes every 15 minutes.
- **The weekly invoice drafts itself.** Every Monday it works out the previous week's
  charges from the actual NetSuite transactions and prices them off the rate card.
- **A person still approves everything.** It creates a *draft* invoice in NetSuite — it
  never issues anything on its own. The draft can be edited first, and one-off charges
  like additional labour or kitting can be added before it goes.
- **It flags when we're about to under-bill.** If a container came in with nothing
  charged against it, the screen says so in red before anyone presses send. That matters
  more than the time saved — a container we forget to bill is $1,500 that nobody notices.

**Where it's up to.** Live on our NetSuite production account, syncing Mova's real data
daily, with the weekly billing drafts generating. Still to come: pushing the first
automated invoice through, giving Mova their own logins, and onboarding the second 3PL
customer.

**What it cost.** Built in-house — no licence fees, no new supplier, no new system to
maintain. It reads the NetSuite data we already keep and writes back into NetSuite, so
there's no second set of numbers to reconcile.

I've attached a few slides with screenshots. Happy to walk either of you through it
whenever suits — it's more convincing to click around than to read about.

Aaron

---

## Notes before you send

- Swap "Hi both" for the actual names.
- The deck's figures are Mova's real position as at the start of August (13,478 units,
  1,112 pallets, 36 containers). Worth a sanity check against NetSuite on the day you
  send, since the numbers move weekly.
- If they'll ask "what happens if it gets a number wrong": the honest answer is that
  NetSuite stays the source of truth, nothing is issued without a person approving it,
  and every charge line traces back to the receipts and fulfilments it came from.
- If you'd rather not raise onboarding a second customer yet, cut that line from *Where
  it's up to* — it invites a "who and when" question.
