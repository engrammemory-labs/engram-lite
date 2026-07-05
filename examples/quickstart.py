"""Smallest possible use of engram as a library.

Run:  python examples/quickstart.py
(After `pip install -e .` — see the repo README.)
"""
from engram import Memory


def main() -> None:
    mem = Memory("quickstart.db")

    # SAVE a few facts (subject pins them into the same "block")
    print(mem.save("Bob owns the payments service", subject="Bob"))
    print(mem.save("Bob is on parental leave until 2026-07-01",
                   subject="Bob", volatility_class="time_bounded",
                   valid_until="2026-07-01"))
    print(mem.save("The payments repo uses pgx + sqlc, no ORM", subject="payments"))

    # SAVE the same fact again → REINFORCE, not a duplicate
    print(mem.save("Bob owns the payments service", subject="Bob"))

    # FIND — keyword + meaning search, merged
    print("\nrecall 'who owns payments?' :")
    for hit in mem.search("who owns payments?", subject="Bob"):
        print("  -", hit["value"])

    mem.close()


if __name__ == "__main__":
    main()
