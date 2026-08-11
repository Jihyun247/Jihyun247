#!/usr/bin/env python3
"""Render an F1 broadcast-style timing tower from GitHub activity.

Reads commit history + language sizes through the GitHub GraphQL API and
rewrites the block between the TELEMETRY markers in the README.

Environment:
  GH_TOKEN     required. A PAT with `repo` scope if you want private repos
               counted; the default Actions token works for public-only.
  GH_USERNAME  required. Profile login, e.g. Jihyun247
  TIMEZONE     IANA name used to bucket commit hours. Default Asia/Seoul.
  README_PATH  Default README.md
  MAX_REPOS    Repos to scan, newest-pushed first. Default 60.
  MAX_PAGES    Commit pages (100 each) per repo. Default 5.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API = "https://api.github.com/graphql"

TOKEN = os.environ.get("GH_TOKEN", "")
USER = os.environ.get("GH_USERNAME", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Seoul"))
README_PATH = os.environ.get("README_PATH", "README.md")
MAX_REPOS = int(os.environ.get("MAX_REPOS", "60"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))

START = "<!--START:TELEMETRY-->"
END = "<!--END:TELEMETRY-->"

# Broadcast graphics palette, kept here so the ASCII and the badges agree.
SECTORS = [
    ("S1", "MORNING", range(6, 12)),
    ("S2", "DAYTIME", range(12, 18)),
    ("S3", "EVENING", range(18, 24)),
    ("S4", "NIGHT", list(range(0, 6))),
]

# Timing tower column widths: position | name | lap | gap
COLS = (4, 18, 10, 11)
INNER = sum(COLS) + len(COLS) - 1  # inside the box borders
WIDTH = INNER + 2                  # including them, so rules line up with the table


# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------

def graphql(query: str, **variables):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        API,
        data=payload,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER}-f1-telemetry",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
            if "errors" in body:
                raise RuntimeError(body["errors"])
            return body["data"]
        except urllib.error.HTTPError as error:
            # 502/403 here is almost always secondary rate limiting.
            if error.code in (403, 502) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("GraphQL retries exhausted")


PROFILE_QUERY = """
query($login: String!, $prq: String!, $mrq: String!, $isq: String!) {
  user(login: $login) {
    id
    name
    login
    createdAt
    followers { totalCount }
    repositories(ownerAffiliations: OWNER, isFork: false) { totalCount }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestReviewContributions
      restrictedContributionsCount
    }
  }
  prs: search(query: $prq, type: ISSUE) { issueCount }
  merged: search(query: $mrq, type: ISSUE) { issueCount }
  issues: search(query: $isq, type: ISSUE) { issueCount }
}
"""

REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(
      first: 100
      after: $cursor
      isFork: false
      ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        owner { login }
        isPrivate
        languages(first: 12, orderBy: { field: SIZE, direction: DESC }) {
          edges { size node { name } }
        }
        defaultBranchRef { name }
      }
    }
  }
}
"""

HISTORY_QUERY = """
query($owner: String!, $name: String!, $author: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor, author: { id: $author }) {
            pageInfo { hasNextPage endCursor }
            nodes { committedDate }
          }
        }
      }
    }
  }
}
"""


def fetch_profile():
    return graphql(
        PROFILE_QUERY,
        login=USER,
        prq=f"author:{USER} type:pr",
        mrq=f"author:{USER} type:pr is:merged",
        isq=f"author:{USER} type:issue",
    )


def fetch_repos():
    repos, cursor = [], None
    while True:
        data = graphql(REPOS_QUERY, login=USER, cursor=cursor)
        page = data["user"]["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"] or len(repos) >= MAX_REPOS:
            break
        cursor = page["pageInfo"]["endCursor"]
    return repos[:MAX_REPOS]


def fetch_commit_hours(repos, author_id):
    """Bucket every authored commit into an hour of the configured timezone."""
    hours = defaultdict(int)
    total = 0
    for repo in repos:
        if not repo.get("defaultBranchRef"):
            continue
        owner, name = repo["owner"]["login"], repo["name"]
        cursor, pages = None, 0
        while pages < MAX_PAGES:
            try:
                data = graphql(
                    HISTORY_QUERY,
                    owner=owner,
                    name=name,
                    author=author_id,
                    cursor=cursor,
                )
            except Exception as error:  # a single unreadable repo must not kill the run
                print(f"  ! skipped {owner}/{name}: {error}", file=sys.stderr)
                break
            target = (data.get("repository") or {}).get("defaultBranchRef") or {}
            history = (target.get("target") or {}).get("history")
            if not history:
                break
            for node in history["nodes"]:
                stamp = datetime.fromisoformat(
                    node["committedDate"].replace("Z", "+00:00")
                ).astimezone(TZ)
                hours[stamp.hour] += 1
                total += 1
            pages += 1
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]
        print(f"  · {owner}/{name}: {total} commits so far", file=sys.stderr)
    return hours, total


def fetch_languages(repos):
    sizes = defaultdict(int)
    for repo in repos:
        for edge in (repo.get("languages") or {}).get("edges", []):
            sizes[edge["node"]["name"]] += edge["size"]
    return sizes


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def cell(text: str, width: int, align: str = "<") -> str:
    text = str(text)
    inner = width - 2
    if len(text) > inner:
        text = text[: inner - 1] + "…"
    return " " + format(text, f"{align}{inner}") + " "


def bar(fraction: float, width: int = 10) -> str:
    filled = round(fraction * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def lap_time(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"


def render_standings(sizes: dict[str, int]) -> list[str]:
    """Language share becomes a constructors' table with derived lap times."""
    ranked = sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:8]
    if not ranked:
        return [" NO TIMED LAPS "]

    total = sum(size for _, size in ranked)
    shares = [(name, size / total * 100) for name, size in ranked]
    top = shares[0][1]
    spread = top - shares[-1][1]
    # Scale the field so last place is ~15s off the leader, like a real session.
    scale = (15.0 / spread) if spread > 0.01 else 0.0

    lines = ["╔" + "═" * INNER + "╗"]
    year = datetime.now(TZ).year
    lines.append("║" + cell(f"FORMULA 1 · {year} DEVELOPER CHAMPIONSHIP", INNER, "^") + "║")
    lines.append("╠" + "╦".join("═" * w for w in COLS) + "╣")

    # Nudge the leader off a round number so the tower reads like a real session.
    leader_seconds = 72.0 + top % 1
    for index, (name, share) in enumerate(shares, start=1):
        seconds = leader_seconds + (top - share) * scale
        gap = "—" if index == 1 else f"+{seconds - leader_seconds:.3f}"
        lines.append(
            "║"
            + cell(f"P{index}", COLS[0])
            + "║"
            + cell(name.upper(), COLS[1])
            + "║"
            + cell(lap_time(seconds), COLS[2], ">")
            + "║"
            + cell(gap, COLS[3], "^")
            + "║"
        )

    lines.append("╚" + "╩".join("═" * w for w in COLS) + "╝")
    return lines


def render_sectors(hours: dict[int, int], total: int) -> list[str]:
    counts = [
        (tag, label, sum(hours.get(hour, 0) for hour in window))
        for tag, label, window in SECTORS
    ]
    best = max((count for _, _, count in counts), default=0)

    lines = [" SECTOR TIMES " + "─" * (WIDTH - 14)]

    for tag, label, count in counts:
        share = (count / total * 100) if total else 0.0
        marker = "  ⏱ PURPLE" if count == best and best > 0 else ""
        lines.append(
            f" {tag}  {label:<8} {bar(count / best if best else 0)}"
            f"  {count:>6} commits  {share:5.1f}%{marker}"
        )

    lines.append("")
    if best == 0:
        lines.append(" 🏳 NO DATA — 세션이 아직 시작되지 않았습니다")
        return lines

    winner = next(label for _, label, count in counts if count == best)
    verdict = {
        "MORNING": "🐤 EARLY BIRD — 최고 기록은 S1에서 나옵니다",
        "DAYTIME": "☀️ DAY RUNNER — 최고 기록은 S2에서 나옵니다",
        "EVENING": "🌆 EVENING SPEC — 최고 기록은 S3에서 나옵니다",
        "NIGHT": "🦉 NIGHT OWL — 최고 기록은 S4에서 나옵니다",
    }[winner]
    lines.append(f" {verdict}")
    return lines


def render_race_control(profile: dict, total_commits: int) -> list[str]:
    user = profile["user"]
    started = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    seasons = max(1, datetime.now(timezone.utc).year - started.year)
    stats = [
        ("GRANDS PRIX", total_commits),
        ("WINS", profile["merged"]["issueCount"]),
        ("PODIUMS", profile["prs"]["issueCount"]),
        ("FASTEST LAPS", profile["issues"]["issueCount"]),
        ("CONSTRUCTORS", user["repositories"]["totalCount"]),
        ("SEASONS", seasons),
    ]
    lines = ["", " RACE CONTROL " + "─" * (WIDTH - 14)]
    for index in range(0, len(stats), 2):
        left = stats[index]
        right = stats[index + 1]
        lines.append(
            f" {left[0]:<14}{left[1]:>7}     {right[0]:<14}{right[1]:>7}"
        )
    return lines


def render() -> str:
    print("→ profile", file=sys.stderr)
    profile = fetch_profile()
    author_id = profile["user"]["id"]

    print("→ repositories", file=sys.stderr)
    repos = fetch_repos()
    print(f"  {len(repos)} repos in scope", file=sys.stderr)

    print("→ commit history", file=sys.stderr)
    hours, total = fetch_commit_hours(repos, author_id)

    print("→ languages", file=sys.stderr)
    sizes = fetch_languages(repos)

    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    body = []
    body += render_standings(sizes)
    body.append("")
    body += render_sectors(hours, total)
    body += render_race_control(profile, total)
    body.append("")
    body.append(f" LAST UPDATED  {stamp}  ·  TZ {TZ.key}")

    return "```\n" + "\n".join(body) + "\n```"


def main() -> int:
    if not TOKEN or not USER:
        print("GH_TOKEN and GH_USERNAME are required", file=sys.stderr)
        return 1

    block = render()
    with open(README_PATH, encoding="utf-8") as handle:
        readme = handle.read()

    if START not in readme or END not in readme:
        print(f"markers {START} / {END} not found in {README_PATH}", file=sys.stderr)
        return 1

    head, rest = readme.split(START, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{START}\n{block}\n{END}{tail}"

    if updated == readme:
        print("no change", file=sys.stderr)
        return 0

    with open(README_PATH, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print("README updated", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
