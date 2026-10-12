module github.com/temporalio/temporal-slo-demo/demo/app

go 1.25.4

// Dependency versions ARE pinned here, together with go.sum.
//
// The original template left them unpinned and let the Docker build run
// `go mod tidy`, on the theory that a pinned template goes stale. In practice
// the unpinned build broke first, and in three separate ways at once:
// the Go toolchain floor moved (SDK v1.47 needs go >= 1.25.4), the m3db fork
// of prometheus_client_golang dragged in a 2021 `google.golang.org/genproto`
// that collides with the modern genproto/googleapis/rpc submodule, and two
// SDK APIs were renamed out from under the code.
//
// A demo that cannot build is worth less than a demo on last quarter's SDK,
// so the resolved graph is committed. To refresh it deliberately, run
// `make deps-refresh` and rebuild — that is a maintenance action with a
// visible diff, not something that happens silently on a customer's laptop
// ten minutes before a demo.
//
// The two LLM SDKs below are needed to BUILD but not to RUN: the agent's
// default provider is the offline mock, so the demo has no API key requirement
// and no egress requirement. Both are pinned for the same reason as everything
// else here — an LLM SDK moves fast enough that an unpinned one would be the
// first thing to break.
//
// go.opentelemetry.io/otel/sdk/metric is the ONLY metrics SDK in this module
// besides Tally, and it exports a disjoint namespace (gen_ai.*). See the long
// note at the top of agent_telemetry.go for why the two coexist rather than one
// replacing the other.

require (
	github.com/anthropics/anthropic-sdk-go v1.69.0
	github.com/grafana/pyroscope-go v1.4.2
	github.com/openai/openai-go/v3 v3.55.0
	github.com/prometheus/client_golang v1.24.1
	github.com/stretchr/testify v1.11.1
	github.com/uber-go/tally/v4 v4.1.17
	go.opentelemetry.io/otel v1.45.0
	go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc v1.45.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.45.0
	go.opentelemetry.io/otel/metric v1.45.0
	go.opentelemetry.io/otel/sdk v1.45.0
	go.opentelemetry.io/otel/sdk/metric v1.45.0
	go.temporal.io/sdk v1.47.0
	go.temporal.io/sdk/contrib/opentelemetry v0.8.1
	go.temporal.io/sdk/contrib/tally v0.2.0
)

require (
	github.com/bahlo/generic-list-go v0.2.0 // indirect
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/buger/jsonparser v1.1.2 // indirect
	github.com/cenkalti/backoff/v5 v5.0.3 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/davecgh/go-spew v1.1.1 // indirect
	github.com/facebookgo/clock v0.0.0-20150410010913-600d898af40a // indirect
	github.com/go-logr/logr v1.4.4 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/gogo/protobuf v1.3.2 // indirect
	github.com/golang/mock v1.6.0 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/grafana/pyroscope-go/godeltaprof v0.1.11 // indirect
	github.com/grpc-ecosystem/go-grpc-middleware/v2 v2.3.2 // indirect
	github.com/grpc-ecosystem/grpc-gateway/v2 v2.29.0 // indirect
	github.com/invopop/jsonschema v0.14.0 // indirect
	github.com/klauspost/compress v1.19.1 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/nexus-rpc/nexus-proto-annotations v0.1.0 // indirect
	github.com/nexus-rpc/sdk-go v0.6.0 // indirect
	github.com/pb33f/ordered-map/v2 v2.3.1 // indirect
	github.com/pkg/errors v0.9.1 // indirect
	github.com/pmezard/go-difflib v1.0.0 // indirect
	github.com/prometheus/client_model v0.6.2 // indirect
	github.com/prometheus/common v0.70.1 // indirect
	github.com/prometheus/procfs v0.21.1 // indirect
	github.com/robfig/cron v1.2.0 // indirect
	github.com/standard-webhooks/standard-webhooks/libraries v0.0.1 // indirect
	github.com/stretchr/objx v0.5.3 // indirect
	github.com/tidwall/gjson v1.19.0 // indirect
	github.com/tidwall/match v1.1.1 // indirect
	github.com/tidwall/pretty v1.2.1 // indirect
	github.com/tidwall/sjson v1.2.5 // indirect
	github.com/twmb/murmur3 v1.1.8 // indirect
	go.opentelemetry.io/auto/sdk v1.2.1 // indirect
	go.opentelemetry.io/otel/exporters/otlp/otlptrace v1.45.0 // indirect
	go.opentelemetry.io/otel/trace v1.45.0 // indirect
	go.opentelemetry.io/proto/otlp v1.11.0 // indirect
	go.temporal.io/api v1.63.4 // indirect
	go.uber.org/atomic v1.11.0 // indirect
	go.yaml.in/yaml/v4 v4.0.0-rc.2 // indirect
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sync v0.22.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.40.0 // indirect
	golang.org/x/time v0.5.0 // indirect
	google.golang.org/genproto/googleapis/api v0.0.0-20260803160001-6ac0973c030d // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260803160001-6ac0973c030d // indirect
	google.golang.org/grpc v1.83.1 // indirect
	google.golang.org/protobuf v1.36.11 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)
