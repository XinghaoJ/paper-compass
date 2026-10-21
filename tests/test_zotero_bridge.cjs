// 用 Node 模拟 Zotero 接口，验证插件协议、下载结果和条目复用；不启动 Zotero、不联网。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

/** 构造独立 Zotero 模拟环境，并加载插件启动脚本。 */
async function setup({ available = true, foreignDOI = false } = {}) {
    const items = new Map();
    const collections = new Map();
    class Collection {
        constructor() { this.id = collections.size + 1; this.key = `COL${this.id}`; }
        async saveTx() { collections.set(this.id, this); }
    }
    let nextID = 1;
    class Item {
        constructor() { this.id = nextID++; this.key = `KEY${this.id}`; this.fields = {}; this.attachments = []; }
        setField(name, value) { this.fields[name] = value; }
        getField(name) { return this.fields[name] || ""; }
        setCreators(creators) { this.creators = creators; }
        addTag() {}
        addToCollection(id) { this.collections = new Set([...(this.collections || []), id]); }
        async saveTx() { items.set(this.id, this); }
        getAttachments() { return this.attachments; }
        isRegularItem() { return true; }
    }
    class Search {
        addCondition(name, op, value) { this.doi = value; }
        async search() { return [...items.values()].filter(item => item.fields?.DOI === this.doi).map(item => item.id); }
    }
    const Zotero = {
        version: "10.0-test", Server: { Endpoints: {} }, Libraries: { userLibraryID: 1 }, Item, Search,
        Collection, Collections: { getByLibrary: id => [...collections.values()].filter(c => c.libraryID === id && !c.parentID && !c.deleted) },
        Items: { getAsync: async id => items.get(id) },
        File: { async getResourceAsync(url) {
            assert.equal(url, "jar:file:///plugin/bridge.xpi!/token.json");
            return JSON.stringify({ token: "a".repeat(64) });
        } },
        Attachments: {
            async addAvailableFile(item, options) {
                assert.deepEqual(Array.from(options.methods), ["doi", "url", "oa"]);
                if (!available) return false;
                const attachment = { id: nextID++, key: "ATTACH", attachmentContentType: "application/pdf",
                                     getFilePathAsync: async () => "/zotero/storage/paper.pdf" };
                items.set(attachment.id, attachment);
                item.attachments.push(attachment.id);
                return attachment;
            }
        },
        Translate: { Search: class {
            setIdentifier(data) { this.doi = data.DOI; }
            async getTranslators() { return ["translator"]; }
            setTranslator() {}
            async translate() { return [{ DOI: foreignDOI ? "10.1234/foreign" : this.doi, title: "Exact DOI paper" }]; }
        } },
        logError(error) { throw error; }
    };
    const context = vm.createContext({ Zotero });
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../zotero_bridge/bootstrap.js"), "utf8"), context);
    await context.startup({ rootURI: "jar:file:///plugin/bridge.xpi!/" });
    const headers = { "X-Literature-Token": "a".repeat(64) };
    const endpoint = new Zotero.Server.Endpoints["/literature-assistant/jobs"]();
    /** 调用插件任务接口，JSON 序列化模拟真实 HTTP 边界。 */
    async function request(data, suppliedHeaders = headers) {
        const result = await endpoint.init({ headers: suppliedHeaders, data });
        return { status: result[0], data: JSON.parse(result[2]) };
    }
    /** 等待模拟后台队列执行完毕，不访问真实网络。 */
    async function flush() { await new Promise(resolve => setImmediate(resolve)); }
    return { context, Zotero, items, collections, request, flush };
}

test("study collections reuse items and PDFs while preserving other memberships", async () => {
    const env = await setup();
    const data = { action: "download", doi: "10.1234/test", title: "Paper", collection_name: "调研甲", job_id: "1".repeat(32) };
    await env.request(data);
    // 相同 DOI 在另一调研也必须执行归类，不能合并为原调研的任务。
    await env.request({ ...data, collection_name: "调研乙", job_id: "2".repeat(32) });
    await env.flush();
    assert.equal(env.collections.size, 2);
    assert.equal(env.items.size, 2);
    const item = [...env.items.values()].find(i => i.fields);
    assert.deepEqual([...item.collections], [1, 2]);
    const status = await env.request({ action: "status", job_id: "2".repeat(32) });
    assert.equal(status.data.collection_name, "调研乙");
    assert.equal(status.data.collection_key, "COL2");
    await env.request({ ...data, job_id: "3".repeat(32) });
    await env.flush();
    assert.equal(env.collections.size, 2);
    assert.equal(env.items.size, 2);
    assert.deepEqual([...item.collections], [1, 2]);
    assert.equal((await env.request({ ...data, collection_name: "调研乙" })).status, 409);
});

test("rejects malformed collection names and retains unavailable items in their study", async () => {
    const env = await setup({ available: false });
    const data = { action: "download", doi: "10.1234/test", title: "Paper", job_id: "1".repeat(32) };
    for (const collection_name of [42, "a\n", "x".repeat(256)]) {
        assert.equal((await env.request({ ...data, collection_name })).status, 400);
    }
    await env.request({ ...data, collection_name: "调研甲" });
    await env.flush();
    const result = await env.request({ action: "status", job_id: data.job_id });
    assert.equal(result.data.status, "unavailable");
    assert.equal(result.data.collection_key, "COL1");
});

test("rejects unauthenticated, browser-origin and malformed requests", async () => {
    const env = await setup();
    const data = { action: "download", doi: "10.1234/test", job_id: "1".repeat(32) };
    assert.equal((await env.request(data, {})).status, 403);
    assert.equal((await env.request(data, { "X-Literature-Token": "a".repeat(64), Origin: "https://example.org" })).status, 403);
    assert.equal((await env.request({ ...data, doi: "not-doi" })).status, 400);
    assert.equal(env.items.size, 0);
});

test("downloads PDF and reuses exact DOI item on next request", async () => {
    const env = await setup();
    const data = { action: "download", doi: "10.1234/test", title: "Scholar title", job_id: "1".repeat(32) };
    assert.equal((await env.request(data)).status, 202);
    await env.flush();
    const result = await env.request({ action: "status", job_id: data.job_id });
    assert.equal(result.data.status, "downloaded");
    assert.equal(result.data.path, "/zotero/storage/paper.pdf");
    assert.equal(env.items.size, 2);
    await env.request({ ...data, job_id: "2".repeat(32) });
    await env.flush();
    assert.equal(env.items.size, 2);
});

test("creating an item without a PDF is reported unavailable", async () => {
    const env = await setup({ available: false });
    const job_id = "1".repeat(32);
    await env.request({ action: "download", doi: "10.1234/test", title: "Paper", job_id });
    await env.flush();
    const result = await env.request({ action: "status", job_id });
    assert.equal(result.data.status, "unavailable");
    assert.equal(result.data.path, undefined);
});

test("identifier lookup must return the exact DOI", async () => {
    const env = await setup({ foreignDOI: true });
    const job_id = "1".repeat(32);
    await env.request({ action: "download", doi: "10.1234/test", job_id });
    await env.flush();
    assert.equal((await env.request({ action: "status", job_id })).data.status, "failed");
    assert.equal(env.items.size, 0);
});

test("request IDs are idempotent and cannot be reused for another DOI", async () => {
    const env = await setup();
    const job_id = "1".repeat(32);
    const data = { action: "download", doi: "10.1234/test", title: "Paper", job_id };
    await env.request(data);
    assert.equal((await env.request({ ...data, doi: "10.1234/other" })).status, 409);
    await env.flush();
    await env.request(data);
    await env.flush();
    assert.equal(env.items.size, 2);
    env.context.shutdown();
    assert.equal(env.Zotero.Server.Endpoints["/literature-assistant/jobs"], undefined);
});
