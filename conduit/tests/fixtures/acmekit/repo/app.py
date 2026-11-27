from acmekit import Client


def run(prompt: str) -> str:
    return Client().complete(prompt)
