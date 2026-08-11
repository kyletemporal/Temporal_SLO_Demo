# The builder tag has to keep pace with the Temporal SDK's minimum Go version.
# go.mod pins no dependency versions (see the comment there), so `go mod tidy`
# always resolves the current SDK — and the alpine images set
# GOTOOLCHAIN=local, which means a too-old builder fails outright rather than
# silently downloading a newer toolchain. If the build dies with
# "toolchain upgrade needed", bump this tag; that is the intended fix.
FROM golang:1.25-alpine AS build

WORKDIR /src

RUN apk add --no-cache git

# Dependencies are pinned (go.mod + go.sum). Downloading them in their own
# layer, before the source is copied, means editing a .go file rebuilds in
# seconds instead of re-fetching the whole module graph.
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
# -mod=readonly: fail loudly if the source and the pinned graph disagree,
# rather than silently mutating go.mod mid-build.
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags="-s -w" -o /out/app .

FROM alpine:3.21

RUN apk add --no-cache ca-certificates && \
    adduser -D -u 10001 app

COPY --from=build /out/app /usr/local/bin/app

USER app

# 8081 = HTTP API (api mode only), 8077 = SDK Prometheus metrics (both modes)
EXPOSE 8081 8077

ENTRYPOINT ["/usr/local/bin/app"]
