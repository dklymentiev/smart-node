#!/bin/bash
#
# DOCI API Integration Tests
#
# Usage:
#   ./tests/api-test.sh
#   DOCI_URL=https://doci.example.com DOCI_API_KEY=xxx ./tests/api-test.sh
#

set -euo pipefail

# Configuration
# Default: direct container access (bypasses Traefik auth)
CONTAINER_IP=$(docker inspect doci --format '{{(index .NetworkSettings.Networks (index (keys .NetworkSettings.Networks) 0)).IPAddress}}' 2>/dev/null || echo "")
BASE_URL="${DOCI_URL:-http://${CONTAINER_IP:-localhost}}"
API_KEY="${DOCI_API_KEY:?DOCI_API_KEY env var required}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Counters
PASSED=0
FAILED=0
TOTAL=0

# Cleanup tracking
CLEANUP_GUIDS=()

# ============================================================================
# Helpers
# ============================================================================

api_get() {
    local path="$1"
    curl -s -w "\n%{http_code}" \
        -H "X-API-Key: $API_KEY" \
        "${BASE_URL}${path}"
}

api_post() {
    local path="$1"
    local data="$2"
    curl -s -w "\n%{http_code}" \
        -X POST \
        -H "X-API-Key: $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$data" \
        "${BASE_URL}${path}"
}

api_post_form() {
    local path="$1"
    shift
    curl -s -w "\n%{http_code}" \
        -X POST \
        -H "X-API-Key: $API_KEY" \
        "$@" \
        "${BASE_URL}${path}"
}

api_put() {
    local path="$1"
    local data="$2"
    curl -s -w "\n%{http_code}" \
        -X PUT \
        -H "X-API-Key: $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$data" \
        "${BASE_URL}${path}"
}

api_delete() {
    local path="$1"
    curl -s -w "\n%{http_code}" \
        -X DELETE \
        -H "X-API-Key: $API_KEY" \
        "${BASE_URL}${path}"
}

api_get_noauth() {
    local path="$1"
    curl -s -w "\n%{http_code}" \
        "${BASE_URL}${path}"
}

# Parse response: last line is HTTP code, rest is body
parse_response() {
    local response="$1"
    HTTP_CODE=$(echo "$response" | tail -n1)
    BODY=$(echo "$response" | sed '$d')
}

# Extract JSON field using grep/sed (no jq dependency)
json_field() {
    local json="$1"
    local field="$2"
    echo "$json" | grep -o "\"${field}\":[^,}]*" | head -1 | sed "s/\"${field}\"://;s/\"//g;s/ //g"
}

# Test assertion
assert_eq() {
    local test_name="$1"
    local expected="$2"
    local actual="$3"
    TOTAL=$((TOTAL + 1))
    if [ "$expected" = "$actual" ]; then
        echo -e "  ${GREEN}[PASS]${NC} $test_name"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}[FAIL]${NC} $test_name (expected: $expected, got: $actual)"
        FAILED=$((FAILED + 1))
    fi
}

assert_contains() {
    local test_name="$1"
    local needle="$2"
    local haystack="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$haystack" | grep -q "$needle"; then
        echo -e "  ${GREEN}[PASS]${NC} $test_name"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}[FAIL]${NC} $test_name (expected to contain: $needle)"
        FAILED=$((FAILED + 1))
    fi
}

assert_not_empty() {
    local test_name="$1"
    local value="$2"
    TOTAL=$((TOTAL + 1))
    if [ -n "$value" ] && [ "$value" != "null" ]; then
        echo -e "  ${GREEN}[PASS]${NC} $test_name"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}[FAIL]${NC} $test_name (value is empty or null)"
        FAILED=$((FAILED + 1))
    fi
}

# ============================================================================
# Test: Health
# ============================================================================

test_health() {
    echo -e "\n${CYAN}=== Health ===${NC}"

    local resp
    resp=$(api_get "/api/health.php")
    parse_response "$resp"

    assert_eq "GET /api/health.php returns 200" "200" "$HTTP_CODE"
    assert_contains "Response contains status ok" '"status"' "$BODY"
}

# ============================================================================
# Test: Auth
# ============================================================================

test_auth() {
    echo -e "\n${CYAN}=== Authentication ===${NC}"

    # No auth -> 401
    local resp
    resp=$(api_get_noauth "/api/documents.php?guid=test")
    parse_response "$resp"

    assert_eq "Request without API key returns 401" "401" "$HTTP_CODE"
    assert_contains "Returns auth error" '"Authentication required"' "$BODY"
}

# ============================================================================
# Test: Documents CRUD
# ============================================================================

test_documents() {
    echo -e "\n${CYAN}=== Documents API ===${NC}"

    local test_path="test/api-test-$(date +%s).md"
    local test_content="# API Test Document\n\nThis is a test document created by api-test.sh"
    local guid=""

    # --- CREATE ---
    local resp
    resp=$(api_post "/api/documents.php" "{\"path\":\"${test_path}\",\"content\":\"${test_content}\",\"title\":\"API Test Doc\"}")
    parse_response "$resp"

    assert_eq "POST create returns 200" "200" "$HTTP_CODE"
    assert_contains "Create returns success" '"success":true' "$BODY"

    guid=$(json_field "$BODY" "guid")
    assert_not_empty "Create returns guid" "$guid"

    if [ -n "$guid" ] && [ "$guid" != "null" ]; then
        CLEANUP_GUIDS+=("$guid")
    fi

    # --- GET by guid ---
    resp=$(api_get "/api/documents.php?guid=${guid}")
    parse_response "$resp"

    assert_eq "GET by guid returns 200" "200" "$HTTP_CODE"
    assert_contains "GET returns correct title" '"API Test Doc"' "$BODY"

    # --- GET by guid with content ---
    resp=$(api_get "/api/documents.php?guid=${guid}&content=1")
    parse_response "$resp"

    assert_eq "GET with content=1 returns 200" "200" "$HTTP_CODE"
    assert_contains "GET includes content field" '"content"' "$BODY"
    assert_contains "Content has expected text" "API Test Document" "$BODY"

    # --- GET by path ---
    resp=$(api_get "/api/documents.php?path=${test_path}")
    parse_response "$resp"

    assert_eq "GET by path returns 200" "200" "$HTTP_CODE"
    assert_contains "GET by path returns correct guid" "$guid" "$BODY"

    # --- UPDATE ---
    local updated_content="# Updated Test Document\n\nContent was updated by api-test.sh"
    resp=$(api_put "/api/documents.php" "{\"guid\":\"${guid}\",\"content\":\"${updated_content}\",\"title\":\"Updated API Test\"}")
    parse_response "$resp"

    assert_eq "PUT update returns 200" "200" "$HTTP_CODE"
    assert_contains "Update returns success" '"success":true' "$BODY"

    # Verify update
    resp=$(api_get "/api/documents.php?guid=${guid}&content=1")
    parse_response "$resp"
    assert_contains "Content was updated" "Updated Test Document" "$BODY"

    # --- Error: POST without path ---
    resp=$(api_post "/api/documents.php" '{"content":"test"}')
    parse_response "$resp"

    assert_contains "POST without path returns error" '"path is required"' "$BODY"

    # --- Error: POST without content ---
    resp=$(api_post "/api/documents.php" '{"path":"test/no-content.md"}')
    parse_response "$resp"

    assert_contains "POST without content returns error" '"content is required"' "$BODY"

    # --- Error: GET nonexistent ---
    resp=$(api_get "/api/documents.php?guid=00000000-0000-0000-0000-000000000000")
    parse_response "$resp"

    # API returns 400 for not found via exception handler (not 404)
    assert_contains "GET nonexistent returns error" '"Document not found"' "$BODY"

    # --- DELETE ---
    resp=$(api_delete "/api/documents.php?guid=${guid}")
    parse_response "$resp"

    assert_eq "DELETE returns 200" "200" "$HTTP_CODE"
    assert_contains "DELETE returns success" '"success":true' "$BODY"

    # Remove from cleanup since already deleted
    CLEANUP_GUIDS=("${CLEANUP_GUIDS[@]/$guid/}")

    # Verify deleted
    resp=$(api_get "/api/documents.php?guid=${guid}")
    parse_response "$resp"

    assert_contains "GET after delete returns not found" '"Document not found"' "$BODY"
}

# ============================================================================
# Test: Threads
# ============================================================================

test_threads() {
    echo -e "\n${CYAN}=== Threads API ===${NC}"

    # Create a document first
    local test_path="test/thread-test-$(date +%s).md"
    local resp
    resp=$(api_post "/api/documents.php" "{\"path\":\"${test_path}\",\"content\":\"# Thread Test\\n\\nSome content to discuss\"}")
    parse_response "$resp"

    local doc_guid
    doc_guid=$(json_field "$BODY" "guid")

    if [ -z "$doc_guid" ] || [ "$doc_guid" = "null" ]; then
        echo -e "  ${RED}[SKIP]${NC} Could not create test document for threads"
        return
    fi
    CLEANUP_GUIDS+=("$doc_guid")

    # --- CREATE thread (discuss document, no quote) ---
    resp=$(api_post "/api/thread.php" "{\"documentGuid\":\"${doc_guid}\",\"comment\":\"What is this document about?\"}")
    parse_response "$resp"

    assert_eq "POST create thread returns 200" "200" "$HTTP_CODE"
    assert_contains "Thread creation returns success" '"success":true' "$BODY"

    local thread_guid
    thread_guid=$(echo "$BODY" | grep -o '"guid":"[^"]*"' | head -1 | sed 's/"guid":"//;s/"//')
    assert_not_empty "Thread has guid" "$thread_guid"

    assert_contains "Thread returns version info" '"version"' "$BODY"

    # --- LIST threads ---
    resp=$(api_get "/api/thread.php?document=${doc_guid}")
    parse_response "$resp"

    assert_eq "GET list threads returns 200" "200" "$HTTP_CODE"
    assert_contains "List returns success" '"success":true' "$BODY"
    assert_contains "List contains threads array" '"threads"' "$BODY"

    # --- Error: POST without documentGuid ---
    resp=$(api_post "/api/thread.php" '{"comment":"test"}')
    parse_response "$resp"

    assert_contains "POST without documentGuid returns error" '"documentGuid is required"' "$BODY"

    # --- Error: POST without comment ---
    resp=$(api_post "/api/thread.php" "{\"documentGuid\":\"${doc_guid}\"}")
    parse_response "$resp"

    assert_contains "POST without comment returns error" '"comment is required"' "$BODY"

    # Cleanup doc (cascades to versions and threads)
    api_delete "/api/documents.php?guid=${doc_guid}" > /dev/null 2>&1
    CLEANUP_GUIDS=("${CLEANUP_GUIDS[@]/$doc_guid/}")
}

# ============================================================================
# Test: Inbox
# ============================================================================

test_inbox() {
    echo -e "\n${CYAN}=== Inbox API ===${NC}"

    # --- ADD ---
    local resp
    resp=$(api_post_form "/api/inbox.php?action=add" -d "content=Test inbox item from api-test.sh" -d "tags=test,api")
    parse_response "$resp"

    assert_eq "POST inbox add returns 200" "200" "$HTTP_CODE"
    assert_contains "Inbox add returns success" '"success":true' "$BODY"

    local inbox_guid
    inbox_guid=$(json_field "$BODY" "guid")
    assert_not_empty "Inbox item has guid" "$inbox_guid"

    if [ -n "$inbox_guid" ] && [ "$inbox_guid" != "null" ]; then
        CLEANUP_GUIDS+=("$inbox_guid")
    fi

    # --- LIST ---
    resp=$(api_get "/api/inbox.php?action=list&limit=5")
    parse_response "$resp"

    assert_eq "GET inbox list returns 200" "200" "$HTTP_CODE"
    assert_contains "Inbox list returns success" '"success":true' "$BODY"
    assert_contains "Inbox list contains items" '"items"' "$BODY"

    # --- GET ---
    if [ -n "$inbox_guid" ] && [ "$inbox_guid" != "null" ]; then
        resp=$(api_get "/api/inbox.php?action=get&guid=${inbox_guid}")
        parse_response "$resp"

        assert_eq "GET inbox item returns 200" "200" "$HTTP_CODE"
        assert_contains "Inbox item contains content" "Test inbox item" "$BODY"
    fi

    # --- Error: ADD without content ---
    resp=$(api_post_form "/api/inbox.php?action=add" -d "content=")
    parse_response "$resp"

    assert_contains "POST inbox without content returns error" '"Content required"' "$BODY"

    # Cleanup
    if [ -n "$inbox_guid" ] && [ "$inbox_guid" != "null" ]; then
        api_delete "/api/documents.php?guid=${inbox_guid}" > /dev/null 2>&1
        CLEANUP_GUIDS=("${CLEANUP_GUIDS[@]/$inbox_guid/}")
    fi
}

# ============================================================================
# Test: Versions
# ============================================================================

test_versions() {
    echo -e "\n${CYAN}=== Versions API ===${NC}"

    # Create a document
    local test_path="test/version-test-$(date +%s).md"
    local resp
    resp=$(api_post "/api/documents.php" "{\"path\":\"${test_path}\",\"content\":\"# Version Test\"}")
    parse_response "$resp"

    local doc_guid
    doc_guid=$(json_field "$BODY" "guid")

    if [ -z "$doc_guid" ] || [ "$doc_guid" = "null" ]; then
        echo -e "  ${RED}[SKIP]${NC} Could not create test document for versions"
        return
    fi
    CLEANUP_GUIDS+=("$doc_guid")

    # List versions (should be empty initially)
    resp=$(api_get "/api/versions.php?document=${doc_guid}")
    parse_response "$resp"

    assert_eq "GET versions returns 200" "200" "$HTTP_CODE"
    assert_contains "Versions returns success" '"success":true' "$BODY"
    assert_contains "Versions contains array" '"versions"' "$BODY"

    # Cleanup
    api_delete "/api/documents.php?guid=${doc_guid}" > /dev/null 2>&1
    CLEANUP_GUIDS=("${CLEANUP_GUIDS[@]/$doc_guid/}")
}

# ============================================================================
# Test: List (folder prefix)
# ============================================================================

test_list_folder() {
    echo -e "\n${CYAN}=== List API (folder prefix) ===${NC}"

    # Seed two docs in a scratch folder so the prefix match is not empty.
    local folder="test-list-folder-$(date +%s)"
    local resp guid1 guid2

    resp=$(api_post "/api/documents.php" "{\"path\":\"${folder}/one.md\",\"content\":\"# one\"}")
    parse_response "$resp"
    guid1=$(json_field "$BODY" "guid")
    [ -n "$guid1" ] && [ "$guid1" != "null" ] && CLEANUP_GUIDS+=("$guid1")

    resp=$(api_post "/api/documents.php" "{\"path\":\"${folder}/nested/two.md\",\"content\":\"# two\"}")
    parse_response "$resp"
    guid2=$(json_field "$BODY" "guid")
    [ -n "$guid2" ] && [ "$guid2" != "null" ] && CLEANUP_GUIDS+=("$guid2")

    # GET /api/list.php?folder=<folder> should return exactly these two docs
    # (recursive by default across sub-folders).
    resp=$(api_get "/api/list.php?folder=${folder}")
    parse_response "$resp"

    assert_eq "GET list?folder=<prefix> returns 200" "200" "$HTTP_CODE"
    assert_contains "list?folder response includes shallow doc" "${folder}/one.md" "$BODY"
    assert_contains "list?folder response includes nested doc" "${folder}/nested/two.md" "$BODY"
    assert_contains "list?folder reports count" '"count":2' "$BODY"

    # A folder that does not exist returns success with zero rows, not 404.
    resp=$(api_get "/api/list.php?folder=this-folder-does-not-exist-${folder}")
    parse_response "$resp"
    assert_eq "GET list?folder=<missing> returns 200" "200" "$HTTP_CODE"
    assert_contains "Empty folder reports count 0" '"count":0' "$BODY"

    # A folder that sanitises to empty (only `.` and `..`) is a 400.
    resp=$(api_get "/api/list.php?folder=..")
    parse_response "$resp"
    assert_eq "GET list?folder=.. returns 400" "400" "$HTTP_CODE"

    # Cleanup handled by CLEANUP_GUIDS.
}

# ============================================================================
# Cleanup & Run
# ============================================================================

cleanup() {
    if [ ${#CLEANUP_GUIDS[@]} -gt 0 ]; then
        echo -e "\n${YELLOW}Cleaning up test data...${NC}"
        for guid in "${CLEANUP_GUIDS[@]}"; do
            if [ -n "$guid" ]; then
                api_delete "/api/documents.php?guid=${guid}" > /dev/null 2>&1 || true
            fi
        done
    fi
}

trap cleanup EXIT

echo -e "${CYAN}DOCI API Integration Tests${NC}"
echo -e "URL: ${BASE_URL}"
echo -e "Auth: API Key"

# Run all tests
test_health
test_auth
test_documents
test_threads
test_inbox
test_versions
test_list_folder

# Summary
echo -e "\n${CYAN}==============================${NC}"
echo -e "Total: ${TOTAL}  ${GREEN}Passed: ${PASSED}${NC}  ${RED}Failed: ${FAILED}${NC}"

if [ "$FAILED" -gt 0 ]; then
    echo -e "${RED}SOME TESTS FAILED${NC}"
    exit 1
else
    echo -e "${GREEN}ALL TESTS PASSED${NC}"
    exit 0
fi
