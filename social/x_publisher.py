import tweepy
import os
import random
import time

class XPublisher:
    def __init__(self):
        # Same OAuth 1.0a tokens, but Client calls v2 API
        self.client = tweepy.Client(
            consumer_key=os.environ.get("X_API_KEY"),
            consumer_secret=os.environ.get("X_API_SECRET"),
            access_token=os.environ.get("X_ACCESS_TOKEN"),
            access_token_secret=os.environ.get("X_ACCESS_SECRET"),
        )
        # v1.1 still needed for media uploads (v2 has no media endpoint)
        auth = tweepy.OAuth1UserHandler(
            os.environ.get("X_API_KEY"), os.environ.get("X_API_SECRET"),
            os.environ.get("X_ACCESS_TOKEN"), os.environ.get("X_ACCESS_SECRET"),
        )
        self.api_v1 = tweepy.API(auth)

    def _upload_media(self, file_path):
        if not os.path.exists(file_path):
            print(f"  [!] Media not found: {file_path}")
            return None
        try:
            return self.api_v1.media_upload(filename=file_path).media_id_string
        except Exception as e:
            print(f"  [!] Media upload error: {e}")
            return None

    def post_thread(self, main_tweet, sub_tweets, comparison_img, disclaimer):
        print("\n--- 📝 PREVIEW OF POST ---")
        print(f"MAIN TWEET:\n{main_tweet}\n[Attach: {comparison_img}]")
        for sub in sub_tweets:
            print(f"\nREPLY TWEET ({sub['ticker']}):\n{sub['text']}\n[Attach: {sub['image']}]")
        print(f"\nFOOTER:\n{disclaimer}\n--------------------------\n")

        print("🚀 Posting Market Analysis Thread to X...")

        try:
            # 1. Main Header
            m_id = self._upload_media(comparison_img)
            response = self.client.create_tweet(
                text=main_tweet,
                media_ids=[m_id] if m_id else None,
            )
            last_id = response.data['id']
            print("  [+] Main header live.")

            # 2. Individual Tickers
            for sub in sub_tweets:
                print("  [wait] 2-5s delay for algorithm...")
                time.sleep(random.randint(2, 5))

                m_id = self._upload_media(sub['image'])
                body = sub['text']
                
                # Hard cap to 280 characters to be safe
                if len(body) > 280:
                    body = body[:277] + "..."

                response = self.client.create_tweet(
                    text=body,
                    in_reply_to_tweet_id=last_id,
                    media_ids=[m_id] if m_id else None,
                )
                last_id = response.data['id']
                print(f"  [+] {sub['ticker']} analysis live.")

            # 3. Footnote
            print("  [wait] Final 2-5s delay before footer...")
            time.sleep(random.randint(2, 5))
            
            self.client.create_tweet(
                text=disclaimer,
                in_reply_to_tweet_id=last_id,
            )
            print("✅ Success. Thread deployed.")

        except Exception as e:
            print(f"❌ Deployment Failed: {e}")
            print("\n💡 TIP: Ensure 'Read and Write' is enabled in X Dev Portal,")
            print("   then regenerate your Access Token & Secret.")
