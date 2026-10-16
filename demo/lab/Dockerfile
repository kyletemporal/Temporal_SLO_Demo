# Builds the lab controller and the Go lab Worker (one binary, two modes).
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
COPY ui ./ui
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags="-s -w" -o /out/lab .

FROM alpine:3.21
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 app
COPY --from=build /out/lab /usr/local/bin/lab
USER app
# 8095 = UI + API (controller), 9464 = SDK Prometheus metrics (worker)
EXPOSE 8095 9464
ENTRYPOINT ["/usr/local/bin/lab"]
