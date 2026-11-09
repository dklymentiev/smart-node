<?php
/**
 * DOCI -- key (canonical) documents helpers.
 *
 * Thin repository layer over the `key_documents` table. Avoids
 * scattering inline PDO + SELECT around the controllers.
 */

require_once __DIR__ . '/../src/config.php';

/**
 * Has this document been marked key in at least one domain?
 *
 * Per-request static cache so a single page load that asks for many
 * documents (sidebar tree, folder cards) does not fan out into N
 * separate SELECTs.
 *
 * @param string $guid
 * @return bool
 */
function is_key_document(string $guid): bool {
    static $cache = null;
    if ($cache === null) {
        $cache = get_all_key_doc_guids();
    }
    return isset($cache[$guid]);
}

/**
 * Full set of GUIDs that have at least one key_documents row, as a
 * map (guid => true). Computed once per request and cached.
 *
 * @return array<string, bool>
 */
function get_all_key_doc_guids(): array {
    static $map = null;
    if ($map !== null) return $map;

    $map = [];
    try {
        $pdo = get_db();
        $stmt = $pdo->query("SELECT DISTINCT guid FROM key_documents");
        if ($stmt) {
            while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
                $map[$row['guid']] = true;
            }
        }
    } catch (Throwable $e) {
        // Table not present yet (migration not applied) -> empty.
    }
    return $map;
}

/**
 * All key_documents rows for a given document GUID.
 *
 * @param string $guid
 * @return array<int, array{id:int, guid:string, domain:string, label:?string, description:?string, update_trigger:?string, created_at:string}>
 */
function get_key_document_records(string $guid): array {
    try {
        $pdo = get_db();
        $stmt = $pdo->prepare("SELECT id, guid, domain, label, description, update_trigger, created_at FROM key_documents WHERE guid = :g ORDER BY domain");
        $stmt->execute([':g' => $guid]);
        return $stmt->fetchAll(PDO::FETCH_ASSOC) ?: [];
    } catch (Throwable $e) {
        return [];
    }
}

/**
 * List key documents, optionally filtered by domain. Joins on
 * documents to surface path + title for UI display.
 *
 * @param string|null $domain
 * @return array
 */
function list_key_documents(?string $domain = null): array {
    try {
        $pdo = get_db();
        if ($domain !== null && $domain !== '') {
            $stmt = $pdo->prepare("
                SELECT k.id, k.guid, k.domain, k.label, k.description, k.update_trigger, k.created_at,
                       d.path, d.title, d.doc_type
                FROM key_documents k
                LEFT JOIN documents d ON d.guid = k.guid AND d.deleted_at IS NULL
                WHERE k.domain = :d
                ORDER BY k.created_at DESC
            ");
            $stmt->execute([':d' => $domain]);
        } else {
            $stmt = $pdo->query("
                SELECT k.id, k.guid, k.domain, k.label, k.description, k.update_trigger, k.created_at,
                       d.path, d.title, d.doc_type
                FROM key_documents k
                LEFT JOIN documents d ON d.guid = k.guid AND d.deleted_at IS NULL
                ORDER BY k.created_at DESC
            ");
        }
        return $stmt ? ($stmt->fetchAll(PDO::FETCH_ASSOC) ?: []) : [];
    } catch (Throwable $e) {
        return [];
    }
}

/**
 * Add a key_documents row. Domain is required and curated by the caller --
 * NOT inferred from the document path.
 *
 * @param array{guid:string, domain:string, label?:string|null, description?:string|null, update_trigger?:string|null} $data
 * @return array{id:int} | array{error:string}
 */
function add_key_document(array $data): array {
    // guid + domain are typed as required in the phpdoc and the API
    // controller validates them before calling. Empty-string defaults
    // would mask a contract violation; let the regex/empty checks
    // below report the bad payload directly.
    $guid = $data['guid'];
    $domain = trim($data['domain']);
    if (!preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $guid)) {
        return ['error' => 'invalid guid'];
    }
    if ($domain === '') {
        return ['error' => 'domain is required'];
    }
    try {
        $pdo = get_db();
        $stmt = $pdo->prepare("INSERT INTO key_documents (guid, domain, label, description, update_trigger) VALUES (:g, :d, :l, :de, :u) RETURNING id");
        $stmt->execute([
            ':g'  => $guid,
            ':d'  => $domain,
            ':l'  => $data['label'] ?? null,
            ':de' => $data['description'] ?? null,
            ':u'  => $data['update_trigger'] ?? null,
        ]);
        $id = (int)$stmt->fetchColumn();
        return ['id' => $id];
    } catch (Throwable $e) {
        return ['error' => $e->getMessage()];
    }
}

/**
 * Remove a key_documents row by id.
 *
 * @param int $id
 * @return array{removed:int} | array{error:string}
 */
function remove_key_document(int $id): array {
    try {
        $pdo = get_db();
        $stmt = $pdo->prepare("DELETE FROM key_documents WHERE id = :i");
        $stmt->execute([':i' => $id]);
        return ['removed' => $stmt->rowCount()];
    } catch (Throwable $e) {
        return ['error' => $e->getMessage()];
    }
}
