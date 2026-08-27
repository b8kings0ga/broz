package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
)

func main() {
	serviceID, deploymentID := os.Getenv("MIM_SERVICE_ID"), os.Getenv("MIM_DEPLOYMENT_ID")
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" { http.NotFound(w, r); return }
		w.Header().Set("content-type", "text/html; charset=utf-8")
		fmt.Fprintf(w, "<!doctype html><title>Broz Binary</title><h1>Broz Binary is live</h1><p>%s</p><p>%s</p>", serviceID, deploymentID)
	})
	status := func(runtime string) http.HandlerFunc { return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("content-type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "service_id": serviceID, "deployment_id": deploymentID, "runtime": runtime})
	}}
	http.HandleFunc("/healthz", status("binary")); http.HandleFunc("/api/status", status("binary"))
	if err := http.ListenAndServe("0.0.0.0:"+os.Getenv("PORT"), nil); err != nil { panic(err) }
}
