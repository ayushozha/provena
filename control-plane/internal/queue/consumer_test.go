package queue

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestWriteItemSchemaStaysCredentialFree(t *testing.T) {
	typeOfItem := reflect.TypeOf(WriteItem{})
	expected := map[string]string{
		"ID":        "id",
		"TenantID":  "tenant_id",
		"Payload":   "payload",
		"CreatedAt": "created_at",
	}
	if typeOfItem.NumField() != len(expected) {
		t.Fatalf("WriteItem has %d fields, want credential-free schema of %d", typeOfItem.NumField(), len(expected))
	}
	for fieldName, jsonName := range expected {
		field, ok := typeOfItem.FieldByName(fieldName)
		if !ok {
			t.Fatalf("WriteItem missing %s", fieldName)
		}
		if tag := field.Tag.Get("json"); tag != jsonName {
			t.Errorf("%s json tag = %q, want %q", fieldName, tag, jsonName)
		}
	}
}

func TestWriteItemDecodesSnakeCaseTenantEnvelope(t *testing.T) {
	var item WriteItem
	if err := json.Unmarshal([]byte(`{"id":"item-a","tenant_id":"tenant-a","payload":"c2FmZQ==","created_at":"2026-07-13T00:00:00Z"}`), &item); err != nil {
		t.Fatal(err)
	}
	if item.ID != "item-a" || item.TenantID != "tenant-a" || string(item.Payload) != "safe" || item.CreatedAt.IsZero() {
		t.Fatalf("unexpected decoded item: %+v", item)
	}
	encoded, err := json.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"authorization", "headers", "token", "principal_id", "role", "groups"} {
		if _, ok := fields[forbidden]; ok {
			t.Fatalf("credential field %q entered WriteItem JSON", forbidden)
		}
	}
}
