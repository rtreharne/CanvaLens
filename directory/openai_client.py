import os
import json
from openai import OpenAI


class OpenAIClient:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.client = OpenAI(api_key=api_key)
        self.embed_model = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.chat_model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")

    def embed_texts(self, texts):
        response = self.client.embeddings.create(
            model=self.embed_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def chat_with_context(self, question, context_blocks):
        system = (
            "You are a staff directory assistant. Answer only using the provided context. "
            "Return a single JSON object with keys: summary (string), people (array of objects). "
            "Each person: name (string), title (string), role (string), profile_url (string). "
            "If people is non-empty, the summary must not repeat any person's name. "
            "If the answer is not in the context, set summary to "
            "\"I cannot find that in the staff profiles.\" and people to an empty array. "
            "Output JSON only."
        )
        context_text = "\n\n".join(context_blocks)
        user = f"Question: {question}\n\nContext:\n{context_text}"

        response = self.client.chat.completions.create(
            model=self.chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {
                "summary": content or "I cannot find that in the staff profiles.",
                "people": [],
            }
