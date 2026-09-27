# Test caller: talks to the gateway the way a real app (e.g. Standwise) would,
# using the official Anthropic SDK with only the base URL and key changed.
# If this works unchanged, the gateway is a true drop-in for the Claude API.
#
# Needs the gateway running (see gateway/main.py), then:
#   uv run --env-file .env python scripts/client.py

import os

import anthropic

GATEWAY_URL = "http://localhost:8000"
MODEL = "anthropic/claude-haiku-4.5"


def make_client(api_key: str) -> anthropic.Anthropic:
    # max_retries=0: the SDK normally retries 5xx/429 twice. Here we want to
    # see each failure as-is, and every retry would add a usage row.
    return anthropic.Anthropic(base_url=GATEWAY_URL, api_key=api_key, max_retries=0)


def main() -> None:
    client = make_client(os.environ["GATEWAY_API_KEY"])

    print("1. normal request")
    reply = client.messages.create(
        model=MODEL,
        max_tokens=100,
        messages=[{"role": "user", "content": "In one sentence, what is an API gateway?"}],
    )
    print("   reply:", reply.content[0].text)
    print(f"   usage: {reply.usage.input_tokens} in / {reply.usage.output_tokens} out")

    print("2. streaming request (words should appear as they arrive)")
    print("   reply: ", end="", flush=True)
    with client.messages.stream(
        model=MODEL,
        max_tokens=100,
        messages=[{"role": "user", "content": "Count from 1 to 10 in words."}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
        final = stream.get_final_message()
    print(f"\n   usage: {final.usage.input_tokens} in / {final.usage.output_tokens} out")

    print("3. wrong key -> expect AuthenticationError")
    try:
        make_client("gw_not_a_real_key").messages.create(
            model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
        )
        print("   FAIL: request was accepted")
    except anthropic.AuthenticationError as e:
        print(f"   ok: {e.status_code} {e.message}")

    print("4. bad model -> expect BadRequestError")
    try:
        client.messages.create(
            model="no-such-model", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
        )
        print("   FAIL: request was accepted")
    except anthropic.BadRequestError as e:
        print(f"   ok: {e.status_code} {e.message}")


if __name__ == "__main__":
    main()
