# Provena Go SDK

`github.com/ayushozha/provena/sdk/go` is the
current Go client module for embedding Provena into another backend service.

## Audience

This guide is for an external Go service engineer consuming Provena from
outside this repository, not for a repo-local contributor reading the source
tree directly.

## Requirements

- Go `1.26+`
- A reachable Provena base URL:
  - Standalone: `http://127.0.0.1:8092`
  - Polyglot gateway: `http://127.0.0.1:8080`

## Module path and versioning caveats

- Exact module path:
  `github.com/ayushozha/provena/sdk/go`
- Import package name: `provena`
- The SDK currently lives in a repository subdirectory module path. Provena does
  not ship a shorter vanity import path yet.
- This repository does not currently publish standalone Go SDK semver tags.
  Until that changes, external consumers should either:
  - Pin a commit pseudo-version after the desired change lands on GitHub.
  - Use a local `replace` directive against a checked-out copy of this repo
    while validating unpublished changes.

Published-commit workflow:

```bash
go get github.com/ayushozha/provena/sdk/go@<commit-or-pseudo-version>
```

Local checkout workflow:

```bash
go mod edit -replace github.com/ayushozha/provena/sdk/go=/path/to/provena/sdk/go
go get github.com/ayushozha/provena/sdk/go
```

## Quickstart

```go
package main

import (
	"fmt"

	provena "github.com/ayushozha/provena/sdk/go"
)

func main() {
	client := provena.NewClient("http://127.0.0.1:8092")

	health, err := client.Health()
	if err != nil {
		panic(err)
	}

	fmt.Println(health["status"])
}
```

Use `NewClientWithAPIKey` when you are calling an authenticated gateway:

```go
client := provena.NewClientWithAPIKey(
	"http://127.0.0.1:8080",
	"prov_live_your_token",
)
```

If you need to inject or rotate the bearer token later, call
`client.SetAPIKey(...)`.

## Scratch consumer workflow

The following flow compiles a minimal consumer outside `sdk/go`:

```bash
mkdir provena-go-scratch
cd provena-go-scratch
go mod init provena-go-scratch
go mod edit -replace github.com/ayushozha/provena/sdk/go=/path/to/provena/sdk/go
go get github.com/ayushozha/provena/sdk/go
```

Then create `main.go`:

```go
package main

import (
	"fmt"

	provena "github.com/ayushozha/provena/sdk/go"
)

func main() {
	client := provena.NewClient("http://127.0.0.1:8092")
	fmt.Printf("%T\n", client)
}
```

Run the consumer with:

```bash
go run .
```

Once the SDK changes you need are pushed to GitHub, replace the local
`replace` directive with a real `go get ...@<commit-or-pseudo-version>` pin.
After the compile-only check passes, point the same consumer at a running
Provena deployment and add `Health`, `CreateMemory`, or `SearchMemories` calls
from the quickstart above.

## Repository smoke check

The repository keeps its Go regression smoke at
`sdk/go/examples/smoke`. It reaches `/healthz`, creates a
memory, and verifies search can find that memory again.

```powershell
cd sdk/go
$env:PROVENA_BASE_URL = "http://127.0.0.1:8092"
go run ./examples/smoke
```

That checked-in smoke currently only requires `PROVENA_BASE_URL`. For an
authenticated deployment, use `NewClientWithAPIKey` or `SetAPIKey` in your own
service or scratch harness. The Go SDK does not currently ship a separate
installed-consumer smoke CLI.
