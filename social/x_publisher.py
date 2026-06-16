import tweepy
import os
import random
import time

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
                # 403 Forbidden is common when X thinks you're a bot/speeding
                wait = (attempt + 1) * 5  # Incremental backoff: 5s, 10s, 15s
                print(f"  [!] Attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
        
        # If all retries fail, raise exception to stop the script
        raise Exception("Failed to post tweet after 3 attempts.")

    def post_thread(self, main_tweet, sub_tweets, comparison_img, disclaimer):
        """Orchestrates the full thread deployment."""
        print("\n--- 📝 PREVIEW OF POST ---")
        print(f"MAIN TWEET:\n{main_tweet}\n[Attach: {comparison_img}]")
        for sub in sub_tweets:
            print(f"\nREPLY TWEET ({sub['ticker']}):\n{sub['text']}\n[Attach: {sub['image']}]")
        print(f"\nFOOTER:\n{disclaimer}\n--------------------------\n")

        print("🚀 Posting Market Analysis Thread to X...")

        try:
            # 1. Post the Main Header Tweet
            m_id = self._upload_media(comparison_img)
            response = self._safe_create_tweet(
                text=main_tweet,
                media_ids=[m_id] if m_id else None,
            )
            last_id = response.data['id']
            print("  [+] Main header live.")

            # 2. Post Individual Ticker Replies
            for sub in sub_tweets:
                # Use a slightly longer delay (2-7s) to stay under X's rate-limit radar
                delay = random.randint(2, 7)
                print(f"  [wait] {delay}s delay for algorithm...")
                time.sleep(delay)

                m_id = self._upload_media(sub['image'])
                body = sub['text']
                
                # Blue checkmark: 4000 char limit
                if len(body) > 4000:
                    body = body[:3997] + "..."

                response = self._safe_create_tweet(
                    text=body,
                    in_reply_to_tweet_id=last_id,
                    media_ids=[m_id] if m_id else None,
                )
                last_id = response.data['id']
                print(f"  [+] {sub['ticker']} analysis live.")

            # 3. Post the Disclaimer Footnote
            print("  [wait] Final delay before footer...")
            time.sleep(random.randint(2, 7))
            
            self._safe_create_tweet(
                text=disclaimer,
                in_reply_to_tweet_id=last_id,
            )
            print("✅ Success. Thread deployed.")

        except Exception as e:
            print(f"❌ Deployment Failed: {e}")
            print("\n💡 TIP: If this was a 403, check your X Developer Portal Permissions.")
