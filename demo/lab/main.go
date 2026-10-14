// Command lab is the Temporal Worker tuning lab: a controller that serves the
// live UI and generates load, and a Go Worker whose options the UI changes at
// runtime. One binary, two modes:
//
//	lab controller   (MODE=controller)
//	lab worker       (MODE=worker)
//
// Unlike a simulator, nothing on screen is modelled: Workflows run on a real
// Temporal Service, every latency and slot count comes from the SDK's own
// metrics, and the queue depth comes from the server.
package main

import (
	"log"
	"os"
	"strconv"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if n, err := strconv.Atoi(os.Getenv(k)); err == nil {
		return n
	}
	return def
}

func main() {
	mode := env("MODE", "")
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	switch mode {
	case "controller":
		runController()
	case "worker":
		runWorker()
	default:
		log.Fatalf("usage: lab controller|worker (got %q)", mode)
	}
}
