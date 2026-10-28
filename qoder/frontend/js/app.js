// Qoder Sites 上后端挂在 Edge Function 路径下；本地 Flask 后端使用 /api 前缀
const API_BASE = window.STUDYMIND_API_BASE || (
    ['5000', '8000'].includes(window.location.port)
        ? window.location.origin + '/api'
        : window.location.origin + '/functions/v1/app'
);
let searchCount = 0;

// --------------- Navigation ---------------

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.page;
        switchPage(page);
    });
});

function switchPage(page) {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelector(`[data-page="${page}"]`).classList.add('active');
    document.getElementById(`page-${page}`).classList.add('active');
}

// --------------- Health Check ---------------

async function checkHealth() {
    try {
        const resp = await fetch(`${API_BASE}/health`);
        const data = await resp.json();
        const indicator = document.getElementById('statusIndicator');
        const dot = indicator.querySelector('.status-dot');
        const text = indicator.querySelector('.status-text');

        if (data.status === 'ok') {
            dot.className = 'status-dot online';
            const parts = [];
            if (data.powercontext) parts.push('PowerContext');
            if (data.seekdb) parts.push('seekdb');
            if (data.mode === 'degraded') {
                text.textContent = '演示模式（边缘函数内存）';
            } else {
                text.textContent = parts.length ? `${parts.join(' + ')} 已连接` : '本地模式运行中';
            }
        } else {
            dot.className = 'status-dot offline';
            text.textContent = '服务未就绪';
        }
        updateStats();
    } catch (e) {
        const indicator = document.getElementById('statusIndicator');
        indicator.querySelector('.status-dot').className = 'status-dot offline';
        indicator.querySelector('.status-text').textContent = '连接失败';
        document.getElementById('deployNotice').style.display = 'block';
    }
}

async function updateStats() {
    try {
        const [matResp, progResp] = await Promise.all([
            fetch(`${API_BASE}/materials`),
            fetch(`${API_BASE}/progress/summary`),
        ]);
        const matData = await matResp.json();
        const progData = await progResp.json();
        document.getElementById('statMaterials').textContent = matData.count || 0;
        document.getElementById('statMemories').textContent = progData.total_memories || 0;
        document.getElementById('statSearches').textContent = searchCount;
    } catch (e) {
        console.error('Stats update failed:', e);
    }
}

// --------------- Sample Data ---------------

async function loadSampleData() {
    try {
        showToast('正在加载示例数据...', 'success');
        const resp = await fetch(`${API_BASE}/load-samples`, { method: 'POST' });
        const data = await resp.json();
        showToast(`已加载 ${data.count} 条示例数据`, 'success');
        updateStats();
    } catch (e) {
        showToast('加载失败: ' + e.message, 'error');
    }
}

// --------------- Materials ---------------

document.getElementById('addMaterialForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
        title: document.getElementById('matTitle').value,
        content: document.getElementById('matContent').value,
        subject: document.getElementById('matSubject').value,
        topic: document.getElementById('matTopic').value,
        type: document.getElementById('matType').value,
        tags: document.getElementById('matTags').value.split(',').map(t => t.trim()).filter(Boolean),
    };
    try {
        const resp = await fetch(`${API_BASE}/materials`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) {
            showToast('资料已保存', 'success');
            e.target.reset();
            updateStats();
        } else {
            const err = await resp.json();
            showToast('保存失败: ' + (err.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
});

// --------------- Search ---------------

async function performSearch() {
    const query = document.getElementById('searchInput').value.trim();
    if (!query) return;

    const subject = document.getElementById('searchSubject').value;
    const searchType = document.getElementById('searchType').value;
    const container = document.getElementById('searchResults');

    container.innerHTML = '<div class="empty-state"><div class="loading"></div><p>正在检索...</p></div>';

    try {
        const params = new URLSearchParams({ q: query, type: searchType, limit: '10' });
        if (subject) params.append('subject', subject);

        const resp = await fetch(`${API_BASE}/materials/search?${params}`);
        const data = await resp.json();
        searchCount++;
        document.getElementById('statSearches').textContent = searchCount;

        if (data.results && data.results.length > 0) {
            container.innerHTML = data.results.map((r, i) => `
                <div class="result-item">
                    <h4>${r.metadata?.title || '结果 ' + (i + 1)}</h4>
                    <div class="result-meta">
                        ${r.metadata?.subject ? `<span class="meta-tag">${r.metadata.subject}</span>` : ''}
                        ${r.metadata?.topic ? `<span class="meta-tag">${r.metadata.topic}</span>` : ''}
                        ${r.metadata?.type ? `<span class="meta-tag">${r.metadata.type}</span>` : ''}
                    </div>
                    <div class="result-content">${escapeHtml(r.content || '')}</div>
                    ${r.score != null ? `<div class="result-score">相关度: ${(1 - r.score).toFixed(4)}</div>` : ''}
                </div>
            `).join('');
        } else {
            container.innerHTML = '<div class="empty-state">未找到相关内容，试试其他关键词</div>';
        }
    } catch (e) {
        container.innerHTML = `<div class="empty-state">检索失败: ${escapeHtml(e.message)}</div>`;
    }
}

document.getElementById('searchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') performSearch();
});

// --------------- Progress ---------------

document.getElementById('progPercent').addEventListener('input', (e) => {
    document.getElementById('progPercentLabel').textContent = e.target.value + '%';
});

document.getElementById('progressForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
        subject: document.getElementById('progSubject').value,
        topic: document.getElementById('progTopic').value,
        progress: parseInt(document.getElementById('progPercent').value),
        notes: document.getElementById('progNotes').value,
    };
    try {
        const resp = await fetch(`${API_BASE}/progress`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) {
            showToast('进度已保存', 'success');
            e.target.reset();
            document.getElementById('progPercentLabel').textContent = '50%';
            updateStats();
            loadProgressSummary();
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
});

document.getElementById('weakPointForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
        subject: document.getElementById('wpSubject').value,
        topic: document.getElementById('wpTopic').value,
        description: document.getElementById('wpDescription').value,
    };
    try {
        const resp = await fetch(`${API_BASE}/weak-points`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) {
            showToast('薄弱点已记录', 'success');
            e.target.reset();
            updateStats();
        }
    } catch (e) {
        showToast('记录失败: ' + e.message, 'error');
    }
});

async function loadProgressSummary() {
    try {
        const resp = await fetch(`${API_BASE}/progress/summary`);
        const data = await resp.json();
        const container = document.getElementById('progressList');

        if (data.memories && data.memories.length > 0) {
            container.innerHTML = data.memories.map(m => {
                const content = m.content || '';
                const progressMatch = content.match(/(\d+)%/);
                const percent = progressMatch ? parseInt(progressMatch[1]) : null;
                const labelMatch = content.match(/Progress: (.+?) =/) || content.match(/Weak point: (.+?) -/);
                const label = labelMatch ? labelMatch[1] : content.substring(0, 60);

                return `
                    <div class="progress-item">
                        <span class="progress-label">${escapeHtml(label)}</span>
                        ${percent != null ? `
                            <div class="progress-bar-container">
                                <div class="progress-bar" style="width: ${percent}%"></div>
                            </div>
                            <span class="progress-percent">${percent}%</span>
                        ` : `
                            <span style="flex:1; font-size:13px; color: var(--text-secondary);">${escapeHtml(content.substring(0, 80))}</span>
                        `}
                    </div>
                `;
            }).join('');
        } else {
            container.innerHTML = '<p class="empty-state">暂无学习记录</p>';
        }
    } catch (e) {
        console.error('Load progress failed:', e);
    }
}

// --------------- Quiz ---------------

document.getElementById('quizForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
        subject: document.getElementById('quizSubject').value,
        topic: document.getElementById('quizTopic').value,
        count: parseInt(document.getElementById('quizCount').value),
    };
    try {
        const resp = await fetch(`${API_BASE}/quiz/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        const container = document.getElementById('quizResult');
        const content = document.getElementById('quizContent');

        if (data.quiz && data.quiz.questions) {
            container.style.display = 'block';
            content.innerHTML = data.quiz.questions.map(q => `
                <div class="quiz-question">
                    <h4>题目 ${q.id}</h4>
                    <p>${escapeHtml(q.prompt)}</p>
                    <span class="source-tag">来源: ${escapeHtml(q.source_material)}</span>
                </div>
            `).join('');
        } else {
            container.style.display = 'block';
            content.innerHTML = '<p class="empty-state">未找到相关材料，请先添加学习资料</p>';
        }
    } catch (e) {
        showToast('生成失败: ' + e.message, 'error');
    }
});

// --------------- Chat / Ask ---------------

async function sendQuestion() {
    const input = document.getElementById('chatInput');
    const question = input.value.trim();
    if (!question) return;

    const messages = document.getElementById('chatMessages');

    // Add user message
    messages.innerHTML += `
        <div class="chat-message user">
            <div class="chat-avatar"><i class="fas fa-user"></i></div>
            <div class="chat-bubble"><p>${escapeHtml(question)}</p></div>
        </div>
    `;
    input.value = '';

    // Add loading
    const loadingId = 'loading-' + Date.now();
    messages.innerHTML += `
        <div class="chat-message assistant" id="${loadingId}">
            <div class="chat-avatar"><i class="fas fa-robot"></i></div>
            <div class="chat-bubble"><div class="loading"></div></div>
        </div>
    `;
    messages.scrollTop = messages.scrollHeight;

    try {
        const resp = await fetch(`${API_BASE}/ask`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question }),
        });
        const data = await resp.json();

        const memCount = data.context_sources?.memories?.length || 0;
        const matCount = data.context_sources?.materials?.length || 0;

        document.getElementById(loadingId).querySelector('.chat-bubble').innerHTML = `
            <p>${escapeHtml(data.answer || '无法生成回答').replace(/\n/g, '<br>')}</p>
            <div class="context-info">
                检索到 ${memCount} 条记忆 + ${matCount} 条资料
                ${data.related_topics?.length ? ' | 相关主题: ' + data.related_topics.join(', ') : ''}
            </div>
        `;
    } catch (e) {
        document.getElementById(loadingId).querySelector('.chat-bubble').innerHTML =
            `<p>回答失败: ${escapeHtml(e.message)}</p>`;
    }
    messages.scrollTop = messages.scrollHeight;
}

document.getElementById('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendQuestion();
});

// --------------- Utilities ---------------

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// --------------- Init ---------------

document.addEventListener('DOMContentLoaded', () => {
    checkHealth();
    setInterval(updateStats, 30000);
});
