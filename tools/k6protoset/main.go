// Command k6protoset writes the FileDescriptorSet k6 needs to call the Temporal
// WorkflowService over gRPC (client.loadProtoset in demo/k6/worker-tuning).
//
// Why a protoset and not .proto files: the Temporal API imports google/api
// annotations and other files k6 does not bundle, so loading .proto sources
// means shipping and path-mapping a tree of them. A protoset is one file, and
// it is generated from the SAME go.temporal.io/api the lab compiles against, so
// it cannot drift from a hand-copied tree.
//
//	go run ./tools/k6protoset demo/k6/worker-tuning/temporal-api.protoset
package main

import (
	"log"
	"os"

	ws "go.temporal.io/api/workflowservice/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/descriptorpb"
)

func main() {
	if len(os.Args) != 2 {
		log.Fatal("usage: k6protoset OUT.protoset")
	}
	set := &descriptorpb.FileDescriptorSet{}
	seen := map[string]bool{}
	// Dependencies before dependents: protoregistry in k6 resolves imports in order.
	var add func(fd protoreflect.FileDescriptor)
	add = func(fd protoreflect.FileDescriptor) {
		if seen[fd.Path()] {
			return
		}
		seen[fd.Path()] = true
		imps := fd.Imports()
		for i := 0; i < imps.Len(); i++ {
			add(imps.Get(i).FileDescriptor)
		}
		set.File = append(set.File, protodesc.ToFileDescriptorProto(fd))
	}
	add(ws.File_temporal_api_workflowservice_v1_service_proto)
	// Deterministic so CI can rebuild this file and byte-compare it with the
	// committed copy: that check is what makes a committed binary reviewable.
	b, err := proto.MarshalOptions{Deterministic: true}.Marshal(set)
	if err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(os.Args[1], b, 0o644); err != nil {
		log.Fatal(err)
	}
	log.Printf("wrote %s: %d files, %d bytes", os.Args[1], len(set.File), len(b))
}
