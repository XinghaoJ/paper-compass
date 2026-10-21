/* 本机自动下载桥接：仅接收带安装令牌的请求，不提供任意代码执行或文件写入接口。 */
var LiteratureBridge;

/** 插件安装入口；下载任务在启动后由本机 Python 客户端提交。 */
function install() {}

/** 启动插件：读取安装时生成的令牌，并注册下载任务接口。 */
async function startup({ rootURI }) {
    // 安装后的 rootURI 为 jar:file: 地址，必须通过资源通道读取，不能按 HTTP 状态码判断。
    const config = JSON.parse(await Zotero.File.getResourceAsync(rootURI + "token.json"));
    const token = config.token;
    if (typeof token !== "string" || token.length < 32) throw new Error("Missing bridge token");
    const jobs = new Map();
    const active = new Map();
    let queue = Promise.resolve();
    const prefix = "/literature-assistant";

    /** 统一生成 JSON HTTP 响应。 */
    function reply(code, data) {
        return [code, "application/json", JSON.stringify(data)];
    }

    /** 验证本机程序的安装令牌，拒绝普通网页请求及未经配对的程序。 */
    function authorized(requestData) {
        const headers = requestData.headers || {};
        const entry = Object.entries(headers).find(([name]) => name.toLowerCase() === "x-literature-token");
        const origin = Object.keys(headers).some(name => name.toLowerCase() === "origin");
        return !origin && entry && entry[1] === token;
    }

    /** 标准化已有 DOI，仅用于完全一致的比较，不进行标题相似度匹配。 */
    function cleanDOI(value) {
        return String(value || "").trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").toLowerCase();
    }

    /** 查找当前条目已有且真实存在的 PDF 附件。 */
    async function findPDF(item) {
        for (const id of item.getAttachments()) {
            const attachment = await Zotero.Items.getAsync(id);
            if (attachment.attachmentContentType !== "application/pdf") continue;
            const path = await attachment.getFilePathAsync();
            if (path) return { path, attachment_key: attachment.key };
        }
        return null;
    }

    /** 按 DOI 复用个人库条目，或从已知 DOI 获取元数据后创建条目。 */
    async function findOrCreateItem(data) {
        const doi = cleanDOI(data.doi);
        const search = new Zotero.Search();
        search.libraryID = Zotero.Libraries.userLibraryID;
        search.addCondition("DOI", "is", doi);
        const ids = await search.search();
        for (const id of ids) {
            const item = await Zotero.Items.getAsync(id);
            if (item.isRegularItem() && !item.deleted && cleanDOI(item.getField("DOI")) === doi) return item;
        }

        let metadata;
        // 有 Scholar 元数据时直接使用，避免不必要的外部元数据查询。
        if (typeof data.title === "string" && data.title.trim()) {
            metadata = { title: data.title, date: String(data.year || ""),
                         abstractNote: String(data.abstract || ""), creators: [] };
            const authors = Array.isArray(data.authors) ? data.authors : [];
            metadata.creators = authors.filter(name => typeof name === "string").map(name => ({
                creatorType: "author", lastName: name, firstName: "", fieldMode: 1
            }));
        }
        else {
            // 只有 DOI 清单时进行精确标识符查询，不使用标题查 DOI。
            const translate = new Zotero.Translate.Search();
            translate.setIdentifier({ DOI: doi });
            const translators = await translate.getTranslators();
            if (!translators.length) throw new Error("No DOI metadata translator available");
            translate.setTranslator(translators);
            const results = await translate.translate({ libraryID: false });
            metadata = results.find(item => cleanDOI(item.DOI) === doi);
            if (!metadata) throw new Error("Identifier lookup returned a different or missing DOI");
        }
        const item = new Zotero.Item("journalArticle");
        item.libraryID = Zotero.Libraries.userLibraryID;
        for (const field of ["title", "date", "abstractNote", "publicationTitle", "volume", "issue", "pages"]) {
            if (typeof metadata[field] === "string" && metadata[field]) item.setField(field, metadata[field]);
        }
        item.setField("DOI", doi);
        item.setField("url", "https://doi.org/" + doi);
        if (metadata.creators?.length) item.setCreators(metadata.creators);
        item.addTag("literature-reading-assistant");
        await item.saveTx();
        return item;
    }

    /** 按调研名称复用个人库顶层分类；加入父条目后，其 PDF 附件随条目显示。 */
    async function addToStudyCollection(item, name) {
        if (!name) return null;
        const libraryID = Zotero.Libraries.userLibraryID;
        let collection = Zotero.Collections.getByLibrary(libraryID).find(c => c.name === name && !c.deleted);
        if (!collection) {
            collection = new Zotero.Collection();
            collection.libraryID = libraryID;
            collection.name = name;
            await collection.saveTx();
        }
        item.addToCollection(collection.id);
        await item.saveTx({ skipDateModifiedUpdate: true });
        return collection;
    }

    /** 执行单篇任务：归入当前调研分类，复用或下载 PDF，保留条目的其他分类。 */
    async function process(job, data) {
        job.status = "running";
        try {
            const item = await findOrCreateItem(data);
            job.item_key = item.key;
            const collection = await addToStudyCollection(item, job.collection_name);
            if (collection) job.collection_key = collection.key;
            let pdf = await findPDF(item);
            if (!pdf) {
                const options = { methods: ["doi", "url", "oa"] };
                if (typeof Zotero.Attachments.addAvailableFile === "function") {
                    await Zotero.Attachments.addAvailableFile(item, options);
                }
                else if (typeof Zotero.Attachments.addAvailablePDF === "function") {
                    await Zotero.Attachments.addAvailablePDF(item, options);
                }
                else throw new Error("This Zotero version has no compatible full-text retrieval API");
                pdf = await findPDF(item);
            }
            if (pdf) Object.assign(job, pdf, { status: "downloaded" });
            else Object.assign(job, { status: "unavailable", error: "Zotero did not obtain an accessible PDF" });
        }
        catch (error) {
            Object.assign(job, { status: "failed", error: String(error.message || error) });
        }
        finally {
            active.delete(JSON.stringify([job.doi, job.collection_name]));
        }
    }

    /** 健康检查接口：确认插件及 Zotero 版本，验证安装令牌是否对应。 */
    function Ping() {}
    Ping.prototype = {
        supportedMethods: ["GET"], supportedDataTypes: ["application/json"], permitBookmarklet: false,
        async init(requestData) {
            if (!authorized(requestData)) return reply(403, { error: "Invalid bridge token" });
            return reply(200, { bridge_version: "0.2.0", zotero_version: Zotero.version, capabilities: ["collections"] });
        }
    };

    /** 任务接口：幂等提交单篇 DOI 任务，或按任务编号查询当前状态。 */
    function Jobs() {}
    Jobs.prototype = {
        supportedMethods: ["POST"], supportedDataTypes: ["application/json"], permitBookmarklet: false,
        async init(requestData) {
            if (!authorized(requestData)) return reply(403, { error: "Invalid bridge token" });
            const data = requestData.data || {};
            if (data.action === "status") {
                const job = jobs.get(data.job_id);
                return job ? reply(200, job) : reply(404, { error: "Unknown job; Zotero may have restarted" });
            }
            const doi = cleanDOI(data.doi);
            if (data.action !== "download" || !/^10\.\d{4,9}\/[^\s<>"?#]+$/i.test(doi)
                || !/^[a-f0-9]{32}$/.test(data.job_id || "")) return reply(400, { error: "Invalid job or DOI" });
            if (data.collection_name !== undefined && (typeof data.collection_name !== "string"
                || data.collection_name.length > 255 || /[\x00-\x1f]/.test(data.collection_name))) {
                return reply(400, { error: "Invalid collection name" });
            }
            const collection_name = (data.collection_name || "").trim();
            const activeKey = JSON.stringify([doi, collection_name]);
            const existing = jobs.get(data.job_id) || jobs.get(active.get(activeKey));
            if (existing) {
                if (existing.doi !== doi || existing.collection_name !== collection_name)
                    return reply(409, { error: "Job DOI or collection mismatch" });
                return reply(200, existing);
            }
            const job = { job_id: data.job_id, doi, collection_name, status: "queued" };
            jobs.set(job.job_id, job);
            active.set(activeKey, job.job_id);
            queue = queue.then(() => process(job, data)).catch(error => Zotero.logError(error));
            return reply(202, job);
        }
    };
    Zotero.Server.Endpoints[prefix + "/ping"] = Ping;
    Zotero.Server.Endpoints[prefix + "/jobs"] = Jobs;
    LiteratureBridge = { prefix };
}

/** 停用插件时撤销 HTTP 接口；已由 Zotero 发出的网络操作可能仍会完成。 */
function shutdown() {
    if (!LiteratureBridge) return;
    delete Zotero.Server.Endpoints[LiteratureBridge.prefix + "/ping"];
    delete Zotero.Server.Endpoints[LiteratureBridge.prefix + "/jobs"];
    LiteratureBridge = undefined;
}

/** 卸载无需删除文献；已下载条目和附件继续保留在用户库中。 */
function uninstall() {}
