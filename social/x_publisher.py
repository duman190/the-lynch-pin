import json
import os
import random
import re
import time

import tweepy

# X allows at most one cashtag per post (403 "Posts are limited to a maximum of one cashtag"),
# counting repeats of the same symbol. "$38.10" is a price, not a cashtag: a letter must follow "$".
CASHTAG_RE = re.compile(r"(?<![\w$])\$(?=[A-Za-z])")

# A thread that failed part-way: the posts not yet live, to finish with `python -m social.x_publisher --resume`
RESUME_PATH = os.path.join("tmp", "x_thread_resume.json")


def strip_cashtags(text):
    """"$NVDA" -> "NVDA" everywhere in ``text``; dollar amounts are left alone."""
    return CASHTAG_RE.sub("", text or "")


def limit_cashtags(text, keep=1):
    """Keeps the first ``keep`` cashtags in ``text`` and turns the rest into plain symbols."""
    seen = 0

    def repl(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= keep else ""
    return CASHTAG_RE.sub(repl, text or "")


class XPublisher:
    def __init__(self):
        # v2 Client for posting tweets
        self.client = tweepy.Client(
            consumer_key=os.environ.get("X_API_KEY"),
            consumer_secret=os.environ.get("X_API_SECRET"),
            access_token=os.environ.get("X_ACCESS_TOKEN"),
            access_token_secret=os.environ.get("X_ACCESS_SECRET"),
        )
        # v1.1 API for media uploads (v2 does not support media upload directly)
        auth = tweepy.OAuth1UserHandler(
            os.environ.get("X_API_KEY"), os.environ.get("X_API_SECRET"),
            os.environ.get("X_ACCESS_TOKEN"), os.environ.get("X_ACCESS_SECRET"),
        )
        self.api_v1 = tweepy.API(auth)

    def _upload_media(self, file_path):
        """Uploads image to X and returns media_id."""
        if not os.path.exists(file_path):
            print(f"  [!] Media not found: {file_path}")
            return None
        try:
            return self.api_v1.media_upload(filename=file_path).media_id_string
        except Exception as e:
            print(f"  [!] Media upload error: {e}")
            return None

    def _safe_create_tweet(self, **kwargs):
        """Retries tweet creation up to 3 times to handle 403 Forbidden or network blips."""
        for attempt in range(3):
            try:
                return self.client.create_tweet(**kwargs)
            except Exception as e:
                if "cashtag" in str(e).lower():
                    raise  # a content rule, not a blip: the same text fails every retry
                # 403 Forbidden is common when X thinks you're a bot/speeding
                wait = (attempt + 1) * 5  # Incremental backoff: 5s, 10s, 15s
                print(f"  [!] Attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
        
        # If all retries fail, raise exception to stop the script
        raise Exception("Failed to post tweet after 3 attempts.")

    @staticmethod
    def preview(main_tweet, sub_tweets, comparison_img, disclaimer):
        """Prints the thread as it would be posted (the run log keeps it; scan_archive --backfill parses it)."""
        main_tweet, disclaimer = limit_cashtags(main_tweet), limit_cashtags(disclaimer)
        print("\n--- 📝 PREVIEW OF POST ---")
        print(f"MAIN TWEET:\n{main_tweet}\n[Attach: {comparison_img}]")
        for sub in sub_tweets:
            print(f"\nREPLY TWEET ({sub['ticker']}):\n{limit_cashtags(sub['text'])}\n[Attach: {sub['image']}]")
        print(f"\nFOOTER:\n{disclaimer}\n--------------------------\n")

    def post_thread(self, main_tweet, sub_tweets, comparison_img, disclaimer, resume_path=RESUME_PATH):
        """Posts the thread; returns True once every post is live.

        If a post fails after its retries, the posts not yet live are saved to ``resume_path`` with the id
        of the last live one, so ``python -m social.x_publisher --resume`` can finish the same thread
        instead of a re-run posting a second one (whose AI text would differ anyway)."""
        self.preview(main_tweet, sub_tweets, comparison_img, disclaimer)
        # Last line of defence for X's one-cashtag rule (AI text can carry extra "$TICKER"s)
        posts = ([{"ticker": None, "text": limit_cashtags(main_tweet), "image": comparison_img}]
                 + [dict(sub, text=limit_cashtags(sub['text'])) for sub in sub_tweets]
                 + [{"ticker": None, "text": limit_cashtags(disclaimer), "image": None, "footer": True}])
        print("🚀 Posting Market Analysis Thread to X...")
        return self._post_posts(posts, None, resume_path)

    def resume(self, resume_path=RESUME_PATH):
        """Finishes a thread a failed run left in ``resume_path``; returns True once it is complete."""
        with open(resume_path, encoding="utf-8") as f:
            state = json.load(f)
        print(f"🔁 Resuming the X thread after post {state['last_id']}: {len(state['posts'])} posts left")
        return self._post_posts(state["posts"], state["last_id"], resume_path)

    def _post_posts(self, posts, last_id, resume_path):
        for i, post in enumerate(posts):
            try:
                if last_id is not None:
                    # Space replies out (1-3 min) to stay under X's rate-limit radar
                    delay = random.randint(60, 180)
                    print(f"  [wait] {delay}s delay for algorithm...")
                    time.sleep(delay)
                m_id = self._upload_media(post['image']) if post.get('image') else None
                body = post['text']
                if len(body) > 4000:  # Blue checkmark: 4000 char limit
                    body = body[:3997] + "..."
                kwargs = {"text": body}
                if m_id:
                    kwargs["media_ids"] = [m_id]
                if last_id is not None:
                    kwargs["in_reply_to_tweet_id"] = last_id
                response = self._safe_create_tweet(**kwargs)
                last_id = response.data['id']
                print(f"  [+] {post['ticker'] + ' analysis' if post.get('ticker') else 'Footer' if post.get('footer') else 'Main header'} live.")
            except Exception as e:
                print(f"❌ Deployment Failed: {e}")
                print("\n💡 TIP: If this was a 403, check your X Developer Portal Permissions.")
                self._save_resume(resume_path, last_id, posts[i:])
                return False
        print("✅ Success. Thread deployed.")
        if resume_path and os.path.exists(resume_path):
            os.remove(resume_path)
        return True

    @staticmethod
    def _save_resume(resume_path, last_id, remaining):
        """Saves the posts not yet live; with no post live there is no thread to resume (re-run instead)."""
        if not resume_path or last_id is None:
            return
        try:
            os.makedirs(os.path.dirname(resume_path) or ".", exist_ok=True)
            with open(resume_path, "w", encoding="utf-8") as f:
                json.dump({"last_id": str(last_id), "posts": remaining}, f, ensure_ascii=False, indent=1)
            print(f"💾 {len(remaining)} posts saved: finish the thread with "
                  f"`python -m social.x_publisher --resume {resume_path}`")
        except OSError as e:
            print(f"  ⚠️ Could not save the rest of the thread: {e}")


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Finish an X thread that a failed run left half-posted")
    ap.add_argument("--resume", default=RESUME_PATH, help=f"resume file (default {RESUME_PATH})")
    sys.exit(0 if XPublisher().resume(ap.parse_args().resume) else 1)
