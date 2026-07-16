import requests
import os
import time
import random


class ThreadsPublisher:
    def __init__(self):
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN")
        self.user_id = os.environ.get("THREADS_USER_ID")
        self.base_url = f"https://graph.threads.net/v1.0/{self.user_id}"

    def _truncate(self, text):
        """Truncate to 499 chars max, replacing last 3 with ... if over."""
        if len(text) <= 499:
            return text
        return text[:496] + "..."

    def _create_container(self, text, topic_tag=None, image_url=None, reply_to=None):
        """Creates a media container and returns its ID."""
        params = {
            "media_type": "IMAGE" if image_url else "TEXT",
            "text": self._truncate(text),
            "access_token": self.access_token,
        }
        if image_url:
            params["image_url"] = image_url
        if topic_tag:
            params["topic_tag"] = topic_tag
        if reply_to:
            params["reply_to_id"] = reply_to

        res = requests.post(f"{self.base_url}/threads", params=params)
        data = res.json()
        if "id" not in data:
            raise Exception(f"Container creation failed: {data}")
        return data["id"]

    def _publish(self, creation_id):
        """Publishes a container and returns the thread ID."""
        params = {
            "creation_id": creation_id,
            "access_token": self.access_token,
        }
        res = requests.post(f"{self.base_url}/threads_publish", params=params)
        data = res.json()
        if "id" not in data:
            raise Exception(f"Publish failed: {data}")
        return data["id"]

    def _safe_post(self, text, topic_tag=None, image_url=None, reply_to=None):
        """Create + publish with retry logic."""
        for attempt in range(3):
            try:
                cid = self._create_container(text, topic_tag, image_url, reply_to)
                # Wait for container to be ready before publishing
                time.sleep(5)
                return self._publish(cid)
            except Exception as e:
                wait = (attempt + 1) * 10
                print(f"  [!] Attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
        raise Exception("Failed to post to Threads after 3 attempts.")

    def post_thread(self, main_tweet, sub_tweets, comparison_img_url, disclaimer, topic_tag=None):
        """Orchestrates the full thread deployment to Threads."""
        print("\n--- 📝 PREVIEW OF THREADS POST ---")
        print(f"MAIN POST:\n{self._truncate(main_tweet)}\n[Tag: {topic_tag}]")
        for sub in sub_tweets:
            print(f"\nREPLY ({sub['ticker']}):\n{self._truncate(sub['text'])}")
        print(f"\nFOOTER:\n{self._truncate(disclaimer)}\n--------------------------\n")

        print("🚀 Posting Analysis Thread to Threads...")

        try:
            # 1. Main post
            last_id = self._safe_post(main_tweet, topic_tag=topic_tag, image_url=comparison_img_url)
            print("  [+] Main header live.")

            # 2. Ticker replies (each tagged with its own ticker)
            for sub in sub_tweets:
                delay = random.randint(5, 10)
                print(f"  [wait] {delay}s delay...")
                time.sleep(delay)

                last_id = self._safe_post(
                    sub['text'],
                    topic_tag=sub.get('topic_tag'),
                    reply_to=last_id,
                    image_url=sub.get('image_url'),
                )
                print(f"  [+] {sub['ticker']} analysis live.")

            # 3. Footer
            time.sleep(random.randint(5, 10))
            self._safe_post(disclaimer, reply_to=last_id)
            print("✅ Success. Threads thread deployed.")

        except Exception as e:
            print(f"❌ Threads Deployment Failed: {e}")
