<?php
/**
 * DOCI - AI Integration
 *
 * Provides AI response functionality for threads using AI Gateway CLI mode.
 * Creates a CLI session, sends message with rich context, gets response, closes session.
 */

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/documents.php';

// AI Gateway configuration (CLI mode)
// AI_GATEWAY_URL is defined in config.php from environment variable
define('AI_GATEWAY_BASE_URL', AI_GATEWAY_URL);
define('AI_GATEWAY_TIMEOUT', (int)(getenv('AI_GATEWAY_TIMEOUT') ?: 120));

// SSL verification - ALWAYS enabled in production
// Can only be disabled with BOTH conditions: DOCI_ENV=development AND AI_GATEWAY_SSL_VERIFY=false
$isDev = getenv('DOCI_ENV') === 'development';
$sslDisableRequested = getenv('AI_GATEWAY_SSL_VERIFY') === 'false';
define('AI_GATEWAY_SSL_VERIFY', !($isDev && $sslDisableRequested));

if (!AI_GATEWAY_SSL_VERIFY) {
    error_log('[DOCI] WARNING: SSL verification disabled for AI Gateway - DEVELOPMENT ONLY');
}

/**
 * Request AI response for a thread
 *
 * @param string $documentGuid GUID of the source document
 * @param string $quote Selected/quoted text from document
 * @param string $comment User's question or comment
 * @param string $threadGuid GUID of the created thread
 * @param string $username User who created the thread
 * @param string $model AI model to use: haiku, sonnet, opus
 * @return array ['success' => bool, 'content' => string, 'error' => string|null]
 */
function request_ai_response(
    string $documentGuid,
    string $quote,
    string $comment,
    string $threadGuid,
    string $username,
    string $model = 'sonnet'
): array {
    doci_log('ai.request.start', [
        'document_guid' => $documentGuid,
        'thread_guid' => $threadGuid,
        'model' => $model,
        'quote_length' => strlen($quote),
        'comment_length' => strlen($comment)
    ]);

    try {
        // Get document metadata
        $document = get_document_by_guid($documentGuid);
        if (!$document) {
            throw new Exception('Document not found');
        }

        // Get document content
        $documentContent = get_document_content($documentGuid);
        if ($documentContent === null) {
            throw new Exception('Could not read document content');
        }

        // Get document hierarchy
        $hierarchy = get_document_hierarchy($documentGuid);

        // Get parent document content (1 level up) for additional context
        $parentContent = null;
        if (!empty($document['parent_guid'])) {
            $parentContent = get_document_content($document['parent_guid']);
            doci_log('ai.request.parent_context', [
                'parent_guid' => $document['parent_guid'],
                'parent_content_length' => $parentContent !== null ? strlen($parentContent) : 0
            ]);
        }

        // Build context
        $context = build_ai_context($document, $hierarchy, $documentContent, $parentContent);

        // Build messages
        $systemPrompt = build_system_prompt($context);
        $userMessage = build_user_message($quote, $comment);

        doci_log('ai.request.context', [
            'hierarchy_depth' => count($hierarchy),
            'document_length' => strlen($documentContent),
            'system_prompt_length' => strlen($systemPrompt),
            'user_message_length' => strlen($userMessage),
            'model' => $model
        ]);

        // Build prompt record for inclusion in thread file
        $promptRecord = "SYSTEM:\n" . $systemPrompt . "\n\nUSER:\n" . $userMessage;

        // Validate input size (prevent excessive AI costs)
        $totalInputLength = strlen($systemPrompt) + strlen($userMessage);
        if ($totalInputLength > 200000) {
            throw new Exception('Input too large for AI processing (' . round($totalInputLength / 1000) . 'KB)');
        }

        // Call AI Gateway (CLI mode - uses short model names: haiku, sonnet, opus)
        $response = call_ai_gateway($systemPrompt, $userMessage, $model);

        if (!$response['success']) {
            throw new Exception($response['error'] ?? 'AI Gateway error');
        }

        doci_log('ai.request.success', [
            'thread_guid' => $threadGuid,
            'response_length' => strlen($response['content'])
        ]);

        // Append AI response to thread file
        $appendResult = append_ai_response_to_thread($threadGuid, $response['content'], $model, $promptRecord);
        if (!$appendResult) {
            doci_log('ai.request.append_failed', ['thread_guid' => $threadGuid], 'WARN');
        }

        return [
            'success' => true,
            'content' => $response['content'],
            'model' => $model
        ];

    } catch (Exception $e) {
        doci_log('ai.request.error', [
            'thread_guid' => $threadGuid,
            'error' => $e->getMessage()
        ], 'ERROR');

        // Append error notice to thread
        append_ai_error_to_thread($threadGuid, $e->getMessage());

        return [
            'success' => false,
            'content' => null,
            'error' => $e->getMessage()
        ];
    }
}

/**
 * Get document content by GUID
 */
function get_document_content(string $guid): ?string {
    $document = get_document_by_guid($guid);
    if (!$document || empty($document['path'])) {
        return null;
    }

    $filePath = dirname(__DIR__) . '/files/' . $document['path'];
    if (!file_exists($filePath)) {
        return null;
    }

    return file_get_contents($filePath);
}

/**
 * Build AI context from document data
 */
function build_ai_context(array $document, array $hierarchy, string $content, ?string $parentContent = null): array {
    // Build hierarchy path (titles only, breadcrumb-style)
    $hierarchyPath = array_map(function($h) {
        return $h['title'] ?? 'Untitled';
    }, $hierarchy);

    // Build hierarchy links
    $hierarchyLinks = array_map(function($h) {
        return '- /' . $h['guid'] . ' - ' . ($h['title'] ?? 'Untitled');
    }, $hierarchy);

    // Build the meaningful ancestor chain: drop self, drop version snapshots
    // (their content is captured by the next layer up that they snapshot).
    // What remains is the alternating thread/document chain that an
    // LLM actually needs to reason about a nested discussion.
    $ancestors = [];
    foreach ($hierarchy as $h) {
        if ($h['guid'] === $document['guid']) continue;
        if (($h['doc_type'] ?? '') === 'version') continue;
        $ancestors[] = [
            'guid'     => $h['guid'],
            'doc_type' => $h['doc_type'] ?? 'document',
            'title'    => $h['title'] ?? 'Untitled',
            'quote'    => $h['quote'] ?? '',
            'depth'    => $h['depth'] ?? 0,
            'content'  => get_document_content($h['guid']),
        ];
    }

    return [
        'title'         => $document['title'] ?? 'Untitled',
        'quote'         => $document['quote'] ?? '',
        'path'          => implode(' > ', $hierarchyPath),
        'links'         => implode("\n", $hierarchyLinks),
        'content'       => $content,
        'parentContent' => $parentContent,
        'doc_type'      => $document['doc_type'] ?? 'document',
        'ancestors'     => $ancestors,
    ];
}

/**
 * Build system prompt with document context
 */
function build_system_prompt(array $context): string {
    $prompt = "You are a helpful assistant in the DOCI documentation system.\n\n";

    $prompt .= "BREADCRUMB:\n" . $context['path'] . "\n\n";

    if (!empty($context['links'])) {
        $prompt .= "ANCESTOR LINKS:\n" . $context['links'] . "\n\n";
    }

    // Walk the ancestor chain root-first so the LLM sees the original
    // document first and drills down into nested threads. Each thread
    // ancestor includes its anchor quote (what the previous discussion
    // was about) and its full conversation log.
    if (!empty($context['ancestors'])) {
        // ancestors are ordered root-first because get_document_hierarchy
        // returns depth DESC, and we preserve that order in build_ai_context.
        foreach ($context['ancestors'] as $a) {
            $label = $a['doc_type'] === 'thread'
                ? sprintf("ANCESTOR THREAD (depth %d)", $a['depth'])
                : sprintf("ROOT DOCUMENT (depth %d)", $a['depth']);
            $prompt .= $label . ": " . $a['title'] . "\n";
            if ($a['quote'] !== '') {
                $prompt .= "Anchored to quote: \"" . $a['quote'] . "\"\n";
            }
            $prompt .= "Full content:\n---\n" . $a['content'] . "\n---\n\n";
        }
    }

    // Current thread/doc — what the user is actually asking on.
    $prompt .= "CURRENT " . strtoupper($context['doc_type']) . ": " . $context['title'] . "\n";
    if (!empty($context['quote'])) {
        $prompt .= "Anchored to quote: \"" . $context['quote'] . "\"\n";
    }
    $prompt .= "Full content:\n---\n" . $context['content'] . "\n---\n\n";

    $prompt .= "INSTRUCTIONS:\n";
    $prompt .= "- Answer the latest user message in the CURRENT discussion above.\n";
    $prompt .= "- Use the ANCESTOR chain to understand what each layer of the discussion is about.\n";
    $prompt .= "- Anchor quotes in each ancestor tell you what passage that layer was opened on.\n";
    $prompt .= "- Reference specific ancestor threads or quotes if your answer builds on them.\n";
    $prompt .= "- Use markdown formatting. Be concise but thorough.\n";
    $prompt .= "- If the answer is not in any of the provided content, say so.\n";

    return $prompt;
}

/**
 * Build user message with quote and question
 */
function build_user_message(string $quote, string $comment): string {
    $message = "";

    if (!empty($quote)) {
        $message .= "SELECTED TEXT FROM DOCUMENT:\n";
        $message .= "> " . str_replace("\n", "\n> ", $quote) . "\n\n";
    }

    $message .= "QUESTION:\n";
    $message .= $comment;

    return $message;
}

/**
 * Whether AI features are wired up. False when AI_GATEWAY_URL or
 * AI_API_KEY are unset -- the API endpoints and UI should hide AI
 * controls in that case.
 *
 * @return bool
 */
function ai_is_configured(): bool {
    if (AI_GATEWAY_URL === '') return false;
    $key = getenv('AI_API_KEY');
    return $key !== false && $key !== '';
}

/**
 * Resolve a friendly model alias ('haiku', 'sonnet', 'opus' or any name)
 * to the provider-specific model identifier read from
 * AI_MODEL_<ALIAS>. Returns null if not configured.
 *
 * @param string $alias
 * @return string|null
 */
function ai_get_model_id(string $alias): ?string {
    $alias = strtolower(preg_replace('/[^a-zA-Z0-9_-]/', '', $alias));
    if ($alias === '') return null;
    $envKey = 'AI_MODEL_' . strtoupper($alias);
    $v = getenv($envKey);
    if ($v === false || $v === '') return null;
    return $v;
}

/**
 * Call an OpenAI-compatible /chat/completions endpoint. Works against
 * OpenAI, OpenRouter, Together.ai, Groq, Fireworks, Ollama, llama.cpp
 * server, vLLM, LM Studio, or any other provider speaking the same
 * protocol.
 *
 * @param string $systemPrompt
 * @param string $userMessage
 * @param string $modelAlias  Friendly alias mapped via AI_MODEL_<ALIAS> env
 * @return array ['success' => bool, 'content' => string|null, 'error' => string|null]
 */
function call_ai_gateway(string $systemPrompt, string $userMessage, string $modelAlias): array {
    if (!ai_is_configured()) {
        return ['success' => false, 'error' => 'AI not configured: set AI_GATEWAY_URL and AI_API_KEY'];
    }

    $modelId = ai_get_model_id($modelAlias);
    if ($modelId === null) {
        return [
            'success' => false,
            'error' => "Model '$modelAlias' not configured: set AI_MODEL_" . strtoupper($modelAlias)
        ];
    }

    $url = rtrim(AI_GATEWAY_URL, '/') . '/chat/completions';
    $apiKey = getenv('AI_API_KEY');

    $payload = [
        'model' => $modelId,
        'messages' => [
            ['role' => 'system', 'content' => $systemPrompt],
            ['role' => 'user',   'content' => $userMessage],
        ],
        'max_tokens' => 4096,
    ];

    doci_log('ai.chat.send', [
        'model_alias' => $modelAlias,
        'model_id' => $modelId,
        'message_length' => strlen($userMessage)
    ]);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => json_encode($payload),
        CURLOPT_HTTPHEADER => [
            'Content-Type: application/json',
            'Authorization: Bearer ' . $apiKey,
        ],
        CURLOPT_TIMEOUT => AI_GATEWAY_TIMEOUT,
        CURLOPT_SSL_VERIFYPEER => AI_GATEWAY_SSL_VERIFY,
        CURLOPT_SSL_VERIFYHOST => AI_GATEWAY_SSL_VERIFY ? 2 : 0,
    ]);

    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $error = curl_error($ch);
    curl_close($ch);

    if ($error) {
        doci_log('ai.chat.curl_error', ['error' => $error], 'ERROR');
        return ['success' => false, 'error' => 'Connection error: ' . $error];
    }
    if ($httpCode >= 400) {
        doci_log('ai.chat.http_error', [
            'http_code' => $httpCode,
            'response' => substr((string)$response, 0, 500)
        ], 'ERROR');
        return ['success' => false, 'error' => "HTTP $httpCode from AI provider"];
    }

    $data = json_decode($response, true);
    if (!is_array($data)) {
        return ['success' => false, 'error' => 'Invalid JSON from AI provider'];
    }
    $content = $data['choices'][0]['message']['content'] ?? '';
    if (!is_string($content) || $content === '') {
        doci_log('ai.chat.empty_response', ['data' => $data], 'WARN');
        return ['success' => false, 'error' => 'Empty response from AI provider'];
    }

    doci_log('ai.chat.success', [
        'response_length' => strlen($content),
        'usage' => $data['usage'] ?? null
    ]);

    return ['success' => true, 'content' => $content];
}

// Legacy session-based ai_gateway_request() was removed; call_ai_gateway()
// now talks directly to an OpenAI-compatible /chat/completions endpoint.

/**
 * Replace AI placeholder with response in thread file
 */
function append_ai_response_to_thread(string $threadGuid, string $content, string $model, string $promptRecord = ''): bool {
    $thread = get_document_by_guid($threadGuid);
    if (!$thread || empty($thread['path'])) {
        return false;
    }

    $filePath = dirname(__DIR__) . '/files/' . $thread['path'];
    if (!file_exists($filePath)) {
        return false;
    }

    $fileContent = file_get_contents($filePath);
    $modelLabel = ucfirst($model);
    $timestamp = date('Y-m-d H:i');

    // Prompt record stays in doci_log only -- writing it into the thread file
    // leaks the entire ancestor chain into the user-visible document, and any
    // <details> markup inside it breaks the wrapping <details> by closing early.
    $aiSection  = "**AI Response** ({$modelLabel}, {$timestamp})\n\n";
    $aiSection .= $content . "\n";

    // Replace placeholder with actual response
    // Note: Use preg_replace_callback to avoid $-escaping issues in AI content
    $placeholder = '/<!-- ai-pending:[a-z]+ -->\n?/';
    if (preg_match($placeholder, $fileContent)) {
        $newContent = preg_replace_callback($placeholder, function() use ($aiSection) {
            return $aiSection;
        }, $fileContent);
        return file_put_contents($filePath, $newContent) !== false;
    }

    // Fallback: append if no placeholder found
    $aiSection = "\n\n---\n\n" . $aiSection;
    return file_put_contents($filePath, $aiSection, FILE_APPEND) !== false;
}

/**
 * Replace AI placeholder with error notice in thread file
 */
function append_ai_error_to_thread(string $threadGuid, string $error): bool {
    $thread = get_document_by_guid($threadGuid);
    if (!$thread || empty($thread['path'])) {
        return false;
    }

    $filePath = dirname(__DIR__) . '/files/' . $thread['path'];
    if (!file_exists($filePath)) {
        return false;
    }

    $fileContent = file_get_contents($filePath);
    $timestamp = date('Y-m-d H:i');

    $errorSection = "_AI was unable to respond ({$timestamp}): {$error}_\n";

    // Replace placeholder with error
    $placeholder = '/<!-- ai-pending:[a-z]+ -->\n?/';
    if (preg_match($placeholder, $fileContent)) {
        $newContent = preg_replace($placeholder, $errorSection, $fileContent);
        return file_put_contents($filePath, $newContent) !== false;
    }

    // Fallback: append if no placeholder found
    $errorSection = "\n\n---\n\n" . $errorSection;
    return file_put_contents($filePath, $errorSection, FILE_APPEND) !== false;
}

/**
 * Continue conversation in a thread
 *
 * @param string $threadGuid GUID of the thread
 * @param string $threadContent Current thread content (before user's new message)
 * @param string $userMessage User's new message
 * @param string|null $originalGuid GUID of the original document (for context)
 * @param string $username User who sent the message
 * @param string $model AI model to use
 * @return array ['success' => bool, 'error' => string|null]
 */
function continue_thread_conversation(
    string $threadGuid,
    string $threadContent,
    string $userMessage,
    ?string $originalGuid,
    string $username,
    string $model = 'sonnet'
): array {
    doci_log('ai.continue.start', [
        'thread_guid' => $threadGuid,
        'original_guid' => $originalGuid,
        'message_length' => strlen($userMessage),
        'model' => $model
    ]);

    try {
        // Build system prompt with thread context
        $systemPrompt = "You are a helpful assistant continuing a conversation in a documentation thread.\n\n";
        $systemPrompt .= "THREAD CONTENT (previous conversation):\n";
        $systemPrompt .= "---\n";
        $systemPrompt .= $threadContent . "\n";
        $systemPrompt .= "---\n\n";

        // Add original document context if available
        if ($originalGuid) {
            $document = get_document_by_guid($originalGuid);
            if ($document) {
                $documentContent = get_document_content($originalGuid);
                if ($documentContent) {
                    $systemPrompt .= "ORIGINAL DOCUMENT: " . ($document['title'] ?? 'Untitled') . "\n";
                    $systemPrompt .= "---\n";
                    $systemPrompt .= $documentContent . "\n";
                    $systemPrompt .= "---\n\n";
                }
            }
        }

        $systemPrompt .= "INSTRUCTIONS:\n";
        $systemPrompt .= "- Continue the conversation naturally\n";
        $systemPrompt .= "- Reference previous messages when relevant\n";
        $systemPrompt .= "- Use markdown formatting\n";
        $systemPrompt .= "- Be concise but thorough\n";

        doci_log('ai.continue.context', [
            'system_prompt_length' => strlen($systemPrompt),
            'model' => $model
        ]);

        // Build prompt record for inclusion in thread file
        $promptRecord = "SYSTEM:\n" . $systemPrompt . "\n\nUSER:\n" . $userMessage;

        // Validate input size
        $totalInputLength = strlen($systemPrompt) + strlen($userMessage);
        if ($totalInputLength > 200000) {
            throw new Exception('Input too large for AI processing (' . round($totalInputLength / 1000) . 'KB)');
        }

        // Call AI Gateway
        $response = call_ai_gateway($systemPrompt, $userMessage, $model);

        if (!$response['success']) {
            throw new Exception($response['error'] ?? 'AI Gateway error');
        }

        doci_log('ai.continue.success', [
            'thread_guid' => $threadGuid,
            'response_length' => strlen($response['content'])
        ]);

        // Append AI response to thread file
        $appendResult = append_ai_response_to_thread($threadGuid, $response['content'], $model, $promptRecord);
        if (!$appendResult) {
            doci_log('ai.continue.append_failed', ['thread_guid' => $threadGuid], 'WARN');
        }

        return [
            'success' => true,
            'content' => $response['content']
        ];

    } catch (Exception $e) {
        doci_log('ai.continue.error', [
            'thread_guid' => $threadGuid,
            'error' => $e->getMessage()
        ], 'ERROR');

        // Append error notice to thread
        append_ai_error_to_thread($threadGuid, $e->getMessage());

        return [
            'success' => false,
            'error' => $e->getMessage()
        ];
    }
}
