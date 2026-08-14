# Go 1.25: go.mod declares 1.25.4, and an older toolchain fails to build rather
# than degrading gracefully.
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/monitor ./cmd/monitor

FROM alpine:3.21
RUN adduser -D -u 10001 monitor
COPY --from=build /out/monitor /usr/local/bin/monitor
USER monitor
EXPOSE 9111
ENTRYPOINT ["/usr/local/bin/monitor"]
