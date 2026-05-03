---
name: api-input-validation
description: Use when validating user input on an API boundary — DRF serializers/validators, regex constraints, normalization, 404 vs 400 vs 422 selection, and existence checks against external systems (e.g. GitHub username lookup). Activates on `serializers.py`, `validators.py`, DRF `Serializer` / `validate_*` methods, or when the user mentions input validation, username validation, or 404 handling.
---

# API Input Validation — Practices

Applies to FR01, FR02, FR07 of `requirements.md` (validate the `username` POST body and the seed-profile existence check).

## 1. Layer responsibilities

- **Serializer**: shape, type, format, length, charset (synchronous, no I/O).
- **View / service layer**: existence checks, authorization, cross-resource consistency (may do I/O).
- **Model**: last-line-of-defense invariants (DB constraints, `clean()`).

Don't put GitHub API calls inside `validate_*` — serializer validation should be cheap and pure. Existence (FR02) belongs in the view.

## 2. GitHub username constraints (FR01)

GitHub username rules:
- 1–39 characters.
- Alphanumeric and single hyphens only.
- Cannot start or end with a hyphen, and cannot contain consecutive hyphens.

Regex: `^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$`

```python
class RecommendRequestSerializer(serializers.Serializer):
    username = serializers.RegexField(
        regex=r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$",
        max_length=39,
        error_messages={"invalid": "Not a valid GitHub username."},
    )

    def validate_username(self, value: str) -> str:
        return value.lower()   # GitHub logins are case-insensitive — normalize early
```

## 3. Status code selection

- **400 Bad Request**: malformed body, missing field, wrong type, regex fail. DRF defaults give this.
- **404 Not Found**: the seed username is well-formed but the GitHub user doesn't exist (FR02). Return from the view, not the serializer.
- **422 Unprocessable Entity**: well-formed body but a business rule fails (e.g. user is suspended, has zero public repos and recommendation is impossible). Use sparingly — many APIs collapse 422 into 400.
- **429**: upstream rate-limited (relay GitHub's, never silently retry forever).
- **502 Bad Gateway**: GitHub returned 5xx and exhausted retries.

## 4. Existence check pattern (FR02)

```python
class RecommendView(APIView):
    def post(self, request):
        ser = RecommendRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)        # → 400
        username = ser.validated_data["username"]

        if not github.user_exists(username):       # cached lookup, see api-rate-limit-resilience
            raise NotFound(f"GitHub user '{username}' not found.")  # → 404

        result = recommend_service.run(username)
        return Response(result)
```

- Cache "user exists / does not exist" briefly (60s) — protects against repeated typos.
- Distinguish "not found" from "rate-limited" from "upstream error" in logs and in the response — they require different client-side actions.

## 5. Normalization

- Lowercase the username before any cache key, BFS visited-set, or Following exclusion comparison. GitHub treats `Octocat` and `octocat` as the same user; your code must too, or FR07's set difference will silently fail.
- Strip surrounding whitespace before regex check (or include `\s*` in the field's `trim_whitespace` behavior — DRF's `CharField` trims by default).

## 6. Error response shape

Pick one shape and use it everywhere:

```json
{
  "error": {
    "code": "user_not_found",
    "message": "GitHub user 'octocat' not found.",
    "details": {"username": "octocat"}
  }
}
```

- Machine-readable `code` for the client; human-readable `message` for logs/debug.
- Don't leak stack traces, GitHub tokens, or internal IDs into `details`.

## 7. Following-set filter (FR07)

- The exclusion set is also user input (transitively): comparisons must use the **normalized** form.
- Build the exclusion as `set[str]` of lowercased logins; compute the candidate list with `candidates - following - {seed}` in one set operation.

## 8. Anti-patterns to flag

- Calling the GitHub API inside `validate_username` — couples validation latency to upstream availability and makes the serializer non-idempotent.
- Returning 200 with `{"error": "..."}` — clients can't distinguish success from failure without parsing the body.
- Returning 400 for a missing GitHub user — that's 404; 400 means the request was malformed.
- Mixing case-sensitive and case-insensitive comparisons across BFS, scoring, and exclusion — silent bug, hard to find.
- Echoing the raw username back in error messages without escaping — XSS risk if rendered in a browser.
- Letting `serializers.CharField` accept up to its default `max_length` (very large) — DRF defaults are not constraints, declare them.
