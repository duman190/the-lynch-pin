import tweepy
import os

class XPublisher:
    def __init__(self):
        # Using OAuth 1.0a (The most stable for Pay-Per-Use)
        self.auth = tweepy.OAuth1UserHandler(
            os.environ.get("X_API_KEY"), os.environ.get("X_API_SECRET"),
            os.environ.get("X_ACCESS_TOKEN"), os.environ.get("X_ACCESS_SECRET")
        )
        self.api = tweepy.API(self.auth)

    def _upload_media(self, file_path):
        if not os.path.exists(file_path):
            print(f"  [!] Media not found: {file_path}")
            return None
        try:
            return self.api.media_upload(filename=file_path).media_id
        except Exception as e:
            print(f"  [!] Media upload error: {e}")
            return None

    def post_thread(self, main_tweet, sub_tweets, comparison_img, disclaimer):
        print("\n--- 📝 PREVIEW OF POST ---")
        print(f"MAIN TWEET:\n{main_tweet}\n[Attach: {comparison_img}]")
        for sub in sub_tweets:
            print(f"\nREPLY TWEET ({sub['ticker']}):\n{sub['text'][:100]}...\n[Attach: {sub['image']}]")
        print(f"\nFOOTER:\n{disclaimer}\n--------------------------\n")

        print("🚀 Posting Market Analysis Thread to X...")
        
        try:
            # 1. Main Header
            m_id = self._upload_media(comparison_img)
            # Use update_status (v1.1) to bypass the v2 Forbidden error
            response = self.api.update_status(status=main_tweet, media_ids=[m_id] if m_id else None)
            last_id = response.id
            print(f"  [+] Main header live.")

            # 2. Individual Tickers
            for sub in sub_tweets:
                m_id = self._upload_media(sub['image'])
                body = sub['text']
                
                # Length check
                if len(body) > 270:
                    body = body[:267] + "..."

                response = self.api.update_status(
                    status=body,
                    in_reply_to_status_id=last_id,
                    auto_populate_reply_metadata=True,
                    media_ids=[m_id] if m_id else None
                )
                last_id = response.id
                print(f"  [+] {sub['ticker']} analysis live.")

            # 3. Footnote
            self.api.update_status(status=disclaimer, in_reply_to_status_id=last_id, auto_populate_reply_metadata=True)
            print("✅ Success. Thread deployed.")

        except Exception as e:
            print(f"❌ Deployment Failed: {e}")
            print("\n💡 TIP: If it's still 403, regenerate your Access Token in X Dev Portal now that you've set 'Read/Write'.")
