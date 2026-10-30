"""GraphQL query + mutation strings for :mod:`awf.common.github_client`.

Extracted from ``github_client.py`` so the client module stays under the
first-party file-size guardrail. These are module-private string constants
consumed only by ``GitHubClient``; they carry no logic and are grouped here
purely for cohesion (one place for the PR-state query and its pagination peers).
"""

from __future__ import annotations

# Minimal lifecycle/identity query for admission paths that need to distinguish
# an open PR from a terminal or deleted one and may need its live head branch
# and target commit.
# Keep this separate from the monitor's full status snapshot so unrelated
# CI/review pagination cannot block admission.
_GQL_PR_LIFECYCLE = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      closed
      merged
      headRefName
      headRefOid
      baseRefOid
    }
  }
}
"""

# GraphQL: fetch PR state + review threads + review comments in one query.
# The changed-file list feeds merge policy, so it is paginated below whenever
# GitHub reports more than the first 100 paths.
_GQL_PR_STATE = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      createdAt
      updatedAt
      headRefName
      headRefOid
      mergeable
      mergeStateStatus
      isDraft
      closed
      merged
      mergeCommit { oid }
      baseRef { name target { ... on Commit { oid } } }
      commits(last: 1) {
        nodes {
          commit {
            committedDate
            statusCheckRollup {
              state
              contexts(first: 100) {
                totalCount
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    startedAt
                    completedAt
                    detailsUrl
                    checkSuite {
                      app {
                        slug
                        name
                      }
                      creator {
                        login
                      }
                    }
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                    creator {
                      login
                    }
                  }
                }
                pageInfo { hasNextPage }
              }
            }
          }
        }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              databaseId
              bodyText
              author { login }
              viewerDidAuthor
              createdAt
              updatedAt
              url
              pullRequestReview { databaseId }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
      reviews(first: 100) {
        nodes {
          databaseId
          body
          state
          submittedAt
          commit { oid }
          url
          author { login }
          authorCanPushToRepository
          viewerDidAuthor
          updatedAt
        }
        pageInfo { hasNextPage endCursor }
      }
      comments(first: 100) {
        nodes {
          databaseId
          body
          isMinimized
          viewerDidAuthor
          createdAt
          updatedAt
          url
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
      files(first: 100) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_FILES_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_REVIEW_THREADS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              databaseId
              bodyText
              author { login }
              viewerDidAuthor
              createdAt
              updatedAt
              url
              pullRequestReview { databaseId }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_REVIEW_THREAD_COMMENTS_PAGE = """
query($threadId: ID!, $cursor: String!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $cursor) {
        nodes {
          databaseId
          bodyText
          author { login }
          viewerDidAuthor
          createdAt
          updatedAt
          url
          pullRequestReview { databaseId }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_REVIEWS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        nodes {
          databaseId
          body
          state
          submittedAt
          commit { oid }
          url
          author { login }
          authorCanPushToRepository
          viewerDidAuthor
          updatedAt
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_ISSUE_COMMENTS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        nodes {
          databaseId
          body
          isMinimized
          viewerDidAuthor
          createdAt
          updatedAt
          url
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


# GraphQL: mutation to resolve a review thread by node ID.
_GQL_RESOLVE_THREAD = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


# GraphQL: reopen a review thread the disposition gate could not verify (#1138),
# so the PR monitor re-triages it instead of asking a human to reopen it.
_GQL_UNRESOLVE_THREAD = """
mutation($threadId: ID!) {
  unresolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


# GraphQL: post a reply INSIDE a review thread (#1018). AWF's disposition reply
# precedes every ``resolveReviewThread`` it issues.
_GQL_ADD_REVIEW_THREAD_REPLY = """
mutation AddReviewThreadReply($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id databaseId }
  }
}
""".strip()


# GraphQL: enumerate EVERY review thread (resolved ones included) for the
# READY_TO_MERGE disposition audit (#1018). Raw ``body`` — not ``bodyText`` — is
# required: ``bodyText`` strips HTML comments, so the AWF marker would vanish.
# ``bodyText`` is read too, to match outside feedback to what ``fetch_pr_status`` lists.
_GQL_REVIEW_THREAD_AUDIT = """
query ReviewThreadDispositionAudit($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          isOutdated
          resolvedBy { login }
          comments(first: 100) {
            nodes { id databaseId body bodyText viewerDidAuthor createdAt lastEditedAt }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_REVIEW_THREAD_AUDIT_ONE = """
query ReviewThreadDispositionAuditOne($threadId: ID!) {
  viewer { login }
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      isOutdated
      resolvedBy { login }
      pullRequest { number repository { nameWithOwner } }
      comments(first: 100) {
        nodes { id databaseId body bodyText viewerDidAuthor createdAt lastEditedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_REVIEW_THREAD_AUDIT_COMMENTS_PAGE = """
query ReviewThreadDispositionAuditComments($threadId: ID!, $cursor: String!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        nodes { id databaseId body bodyText viewerDidAuthor createdAt lastEditedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()
