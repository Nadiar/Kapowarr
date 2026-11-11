---
hide:
  - navigation
---

# API documentation

Kapowarr has a JSON API, which the web UI is built on. Every endpoint is
described in an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document.

## Browsing the API

Open `/api-docs` on your Kapowarr instance, for example
`http://localhost:5656/api-docs`. The page lists every endpoint with its
parameters and responses. If you're logged in to Kapowarr in the same browser,
your API key is filled in, so "Try it out" sends real requests to your
instance. Be careful with endpoints that change or delete data.

The raw document is at `/static/openapi.yaml`. Import it into a tool such as
Postman or Insomnia, or use it to generate a client.

## The basics

- **Authentication:** add `?api_key=<key>` to every request. The key is under
  Settings → General. `POST /api/auth` and `GET /api/public` don't need it.
- **Responses:** every JSON response looks like
  `{"error": null, "result": ...}`. On an error, `error` holds its name (such
  as `VolumeNotFound`) and the HTTP status code says what kind of error it is.
- **Request bodies** are JSON, except the database upload, which is a
  multipart form.
