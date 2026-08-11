package main

import (
	"log"
	"os"
	"time"

	"go.temporal.io/sdk/client"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)

	switch mode := os.Getenv("MODE"); mode {
	case "api":
		log.SetPrefix("[api] ")
		runAPI()
	case "worker":
		log.SetPrefix("[worker] ")
		runWorker()
	default:
		log.Fatalf("MODE must be 'api' or 'worker', got %q", mode)
	}
}

// dialTemporal retries the initial connection.
//
// Compose health checks get the Temporal container to "healthy", but the
// Frontend Service can still refuse connections for a few seconds afterward
// while namespace registration settles. Crash-looping on that produces a
// confusing first-run experience, so we back off instead.
func dialTemporal(opts client.Options) (client.Client, error) {
	const attempts = 30

	var lastErr error
	for i := 1; i <= attempts; i++ {
		c, err := client.Dial(opts)
		if err == nil {
			log.Printf("connected to Temporal at %s (namespace %s)", opts.HostPort, opts.Namespace)
			return c, nil
		}
		lastErr = err
		log.Printf("Temporal not ready (attempt %d/%d): %v", i, attempts, err)
		time.Sleep(2 * time.Second)
	}
	return nil, lastErr
}
