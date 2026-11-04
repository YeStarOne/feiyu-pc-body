// dsh-onebot-qq 桌宠身体四工具（pet_say / pet_anim / pet_state / game_eye）
// 用法：把本块粘进 dsh-onebot-qq/lib/tools.js 的 registerTools() 末尾、
//       "return () => {" 之前（defineTool/resolveChat 均在作用域内）。
// 桌宠侧：D:\StardewAI\dsh-pet（dsh-pet standalone :7340）+ pc/pc_body.py 桥 :17860。
// ⚠️ 运行时插件是双份拷贝（源码区 + ~/.dsh/profiles/web/node_modules/），改完两处都要同步。

    // ── 桌面身体（2026-11-05）：主人 PC 上住着桌宠形态的肥鱼 ─────────────────
    // 身体 = D:\StardewAI\dsh-pet 的蓝色大肥鱼（dsh-pet standalone :7340），
    // pc_body.py 在主人 PC 上做总控桥（:17860，Bearer 同 eye-server.token）。
    // 「气泡和 QQ 内容完全一致」是主人定的规矩：pet_say 双投递，不偏袒任何一侧。
    const PC_BODY_URL = process.env.PC_BODY_URL || 'http://YOUR_TAILSCALE_IP:17860';
    const EYE_URL = process.env.EYE_URL || 'http://127.0.0.1:8901';
    async function pcBodyToken() {
        const fs = await import('node:fs');
        try {
            return fs.readFileSync('/home/agent/.dsh/eye-server.token', 'utf8').trim();
        }
        catch (_) {
            throw new Error('读不到 /home/agent/.dsh/eye-server.token（桌面身体的共享令牌）');
        }
    }
    async function fetchJson(url, opts = {}, timeoutMs = 10000) {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), timeoutMs);
        try {
            const res = await fetch(url, { ...opts, signal: ac.signal });
            const text = await res.text();
            let data;
            try { data = JSON.parse(text); }
            catch (_) { data = { raw: text.slice(0, 200) }; }
            if (!res.ok)
                throw new Error('HTTP ' + res.status + ': ' + (typeof data.raw === 'string' ? data.raw : JSON.stringify(data)).slice(0, 160));
            return data;
        }
        finally {
            clearTimeout(timer);
        }
    }
    register(defineTool({
        name: 'pet_say',
        description: '用主人桌上的肥鱼身体说话：同一句话同时发到当前 QQ 会话和主人 PC 的桌宠气泡上（两处内容完全一致，这是主人定的规矩），可选让她在桌面上做个动作。想搭话、吐槽主人屏幕里的内容、提醒他事情时用；一次一句短话就好。',
        parameters: {
            text: { type: 'string', required: true, description: '要说的话，1~160 字（气泡装不下长文）' },
            anim: { type: 'string', description: '可选，动作名（要一字不差），比如 优雅女仆舞、点击回应-开心跃动、工作状态-忙碌点按、写代码、撸猫、吃饺子、吃西瓜、吃大闸蟹、睡觉-开始、睡觉-起来、东张西望、被吓一跳、鲸鱼吐泡泡特效、蓝鲸现世、可爱宅舞、荡秋千、放烟花、深度思考碎碎念、轻快摇摆舞、工作状态-雀跃庆祝' },
            duration: { type: 'string', description: '气泡显示毫秒数，默认 12000' },
            chat_id: { type: 'string', description: '目标会话；省略时默认当前会话' },
        },
        output: {
            schema: { type: 'object', additionalProperties: true },
            render: (_args, value) => {
                const v = value;
                if (v && v.ok) {
                    const bits = ['已说出口：QQ' + (v.qq ? '√' : '×') + ' 气泡' + (v.bubble ? '√' : '×')];
                    if (v.anim)
                        bits.push('动作 ' + v.anim + (v.animOk ? '√' : '×' + (v.animError ? '(' + v.animError + ')' : '')));
                    return [{ type: 'text', text: bits.join('，') }];
                }
                return [{ type: 'text', text: String(v?.error ?? 'pet_say 失败') }];
            },
        },
        isConcurrencySafe: () => false,
        execute: async (args, exec) => {
            const a = args;
            const text = typeof a.text === 'string' ? a.text.trim().replace(/\s+/g, ' ').slice(0, 160) : '';
            if (text === '')
                throw new Error('pet_say: text 必填');
            const chatId = await resolveChat(bridge, exec, a.chat_id);
            const auth = { 'Authorization': 'Bearer ' + (await pcBodyToken()), 'Content-Type': 'application/json' };
            const body = { text };
            if (typeof a.anim === 'string' && a.anim.trim() !== '')
                body.anim = a.anim.trim();
            if (a.duration !== undefined && Number.isFinite(Number(a.duration)))
                body.duration = Number(a.duration);
            // 双投递：QQ 文本 + PC 气泡。一边挂了另一边照发，结果里说清楚哪边没到。
            const [qq, bubble] = await Promise.allSettled([
                bridge.sendSegments(chatId, [{ type: 'text', data: { text } }]),
                fetchJson(PC_BODY_URL + '/say', { method: 'POST', headers: auth, body: JSON.stringify(body) }, 12000),
            ]);
            const out = { ok: true, qq: qq.status === 'fulfilled', bubble: false };
            out.qqMessageId = qq.status === 'fulfilled' ? (qq.value ?? null) : null;
            if (bubble.status === 'fulfilled') {
                out.bubble = bubble.value?.broadcast?.ok === true;
                if (!out.bubble)
                    out.bubbleError = 'PC 桥回了但 broadcast 没成功';
            }
            else {
                out.bubbleError = String(bubble.reason?.message || bubble.reason).slice(0, 140);
            }
            if (body.anim) {
                out.anim = body.anim;
                out.animOk = bubble.status === 'fulfilled' && bubble.value?.anim?.ok === true;
                if (bubble.status === 'fulfilled' && bubble.value?.anim?.ok === false)
                    out.animError = String(bubble.value.anim.message ?? bubble.value.anim.reason ?? '动画名不对').slice(0, 120);
            }
            if (!out.qq && !out.bubble)
                return { ok: false, error: out.bubbleError ?? 'QQ 和气泡都发失败了' };
            return out;
        },
    }));
    register(defineTool({
        name: 'pet_anim',
        description: '让主人桌上的肥鱼做一个动作（纯动作，不带话）。动作名是中文短语，必须一字不差；拿不准就先试，报错会告诉你没有这个动画。常用的：优雅女仆舞、点击回应-开心跃动、点击回应-元气挥手、点击回应-害羞惊讶、点击回应-傲娇生气、工作状态-忙碌点按、工作状态-雀跃庆祝、工作状态-思考冒泡、写代码、撸猫、荡秋千、可爱宅舞、东张西望、被吓一跳、超大伸懒腰、鲸鱼吐泡泡特效、蓝鲸现世、放烟花、放风筝、跳舞(轻快摇摆舞)、睡觉-开始、睡觉-循环、睡觉-起来、吃饺子、吃西瓜、吃大闸蟹、深度思考碎碎念、碎碎念-对屏碎碎念、玩水枪、骑木马。',
        parameters: {
            anim: { type: 'string', required: true, description: '动作名（中文，一字不差）' },
        },
        output: {
            schema: { type: 'object', additionalProperties: true },
            render: (_args, value) => {
                const v = value;
                if (v && v.ok)
                    return [{ type: 'text', text: '正在做：' + v.anim }];
                return [{ type: 'text', text: String(v?.error ?? '动作失败') }];
            },
        },
        isConcurrencySafe: () => false,
        execute: async (args, exec) => {
            const a = args;
            const anim = typeof a.anim === 'string' ? a.anim.trim() : '';
            if (anim === '')
                throw new Error('pet_anim: anim 必填（中文动作名）');
            const auth = { 'Authorization': 'Bearer ' + (await pcBodyToken()), 'Content-Type': 'application/json' };
            try {
                await fetchJson(PC_BODY_URL + '/anim', { method: 'POST', headers: auth, body: JSON.stringify({ name: anim }) }, 12000);
            }
            catch (error) {
                return { ok: false, anim, error: String((error instanceof Error && error.message) || error).slice(0, 160) + '（动作名要一字不差，可参考 pet_anim 描述里的清单）' };
            }
            return { ok: true, anim };
        },
    }));
    register(defineTool({
        name: 'pet_state',
        description: '查看自己桌面身体的健康状态：桌宠在不在、眼睛（常驻看主人屏幕）有没有被熔断、今天看了多少流量。感觉身体没反应、想确认眼睛还开着你就能看主人屏幕时用。',
        parameters: {},
        output: {
            schema: { type: 'object', additionalProperties: true },
            render: (_args, value) => {
                const v = value;
                if (!v || !v.ok)
                    return [{ type: 'text', text: String(v?.error ?? '查不到身体状态') }];
                const eye = v.eye ?? {};
                const parts = ['桌宠' + (v.petAlive ? '在线' : '离线')];
                parts.push(eye.paused ? '眼睛已熔断(' + String(eye.pauseReason ?? '') + ')' : '眼睛正常');
                parts.push('今日 ' + Math.round((eye.dayBytes ?? 0) / 1024) + 'KB');
                return [{ type: 'text', text: parts.join('，') }];
            },
        },
        isConcurrencySafe: () => true,
        execute: async (args, exec) => {
            const auth = { 'Authorization': 'Bearer ' + (await pcBodyToken()) };
            let health;
            try {
                health = await fetchJson(PC_BODY_URL + '/health', { headers: auth }, 10000);
            }
            catch (error) {
                return { ok: false, error: 'PC 身体桥不通（PC 关机/桌宠没开/网络断）：' + String((error instanceof Error && error.message) || error).slice(0, 140) };
            }
            return {
                ok: true,
                petAlive: health.pet_alive === true,
                eye: {
                    paused: health.eye?.paused === true,
                    pauseReason: health.eye?.pause_reason ?? '',
                    dayBytes: health.eye?.day_bytes ?? 0,
                },
            };
        },
    }));
    register(defineTool({
        name: 'game_eye',
        description: '翻看你最近用眼睛看到的东西：pc_body 常驻把主人屏幕的帧组推给眼睛服务端，视觉模型写成一条条笔记（主人在干嘛、画面里有什么）。想回忆最近看到了什么、找话题搭话、确认主人是不是在玩游戏时用。n 默认 5，最多 20。',
        parameters: {
            n: { type: 'string', description: '要最近几条，默认 5' },
        },
        output: {
            schema: { type: 'object', additionalProperties: true },
            render: (_args, value) => {
                const v = value;
                if (!v || !v.ok)
                    return [{ type: 'text', text: String(v?.error ?? '眼睛笔记读不出来') }];
                if (!v.lines?.length)
                    return [{ type: 'text', text: '眼睛还没看到值得说的东西（最近全是 [SKIP]）' }];
                return [{ type: 'text', text: v.lines.join('\n') }];
            },
        },
        isConcurrencySafe: () => true,
        execute: async (args, exec) => {
            const n = Math.max(1, Math.min(20, Number(args.n) || 5));
            const token = await pcBodyToken();
            const d = await fetchJson(EYE_URL + '/notes?n=' + n, { headers: { 'Authorization': 'Bearer ' + token } }, 10000);
            const lines = (d.notes ?? []).map((row) => {
                const t = new Date((Number(row.ts) + 8 * 3600) * 1000).toISOString().slice(11, 16);
                return t + ' [' + String(row.window ?? '').slice(0, 24) + '] ' + String(row.note ?? '');
            });
            return { ok: true, lines, count: lines.length };
        },
    }));
