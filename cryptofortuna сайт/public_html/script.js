async function loadStats() {
    const participantsEl = document.getElementById('live-participants');
    const limitEl = document.getElementById('live-limit');
    const bankEl = document.getElementById('live-bank');
    const paidEl = document.getElementById('live-paid');
    const drawsEl = document.getElementById('live-draws');
    const walletBalEl = document.getElementById('live-wallet-balance');
    const walletLinkEl = document.getElementById('wallet-bscscan-link');

    try {
        const response = await fetch('https://cryptofortunabot.onrender.com/stats');
        const data = await response.json();

        if (participantsEl) participantsEl.innerText = data.current_participants ?? 0;
        if (limitEl) limitEl.innerText = data.current_limit ?? '—';
        if (bankEl) bankEl.innerText = (data.current_bank ?? 0) + ' USDT';
        if (paidEl) paidEl.innerText = Math.round((data.total_commission || 0) * 9) + ' USDT';
        if (drawsEl) drawsEl.innerText = data.total_draws ?? 0;
        if (walletBalEl) walletBalEl.innerText = data.wallet_balance != null ? data.wallet_balance.toFixed(2) : '—';
        if (walletLinkEl && data.wallet_address) walletLinkEl.href = 'https://bscscan.com/address/' + data.wallet_address;
    } catch (error) {
        console.log('Статистика временно недоступна');
    }
}

async function loadHistory() {
    const list = document.getElementById('history-list');
    if (!list) return;
    const lang = (document.documentElement.lang || 'en').slice(0, 2);

    try {
        const response = await fetch('https://cryptofortunabot.onrender.com/api/history');
        const draws = await response.json();

        if (!Array.isArray(draws) || draws.length === 0) {
            list.innerHTML = '<div class="history-empty">' +
                (lang === 'ru' ? 'Розыгрышей пока не было.' : 'No draws have taken place yet.') +
                '</div>';
            return;
        }

        const membersWord = lang === 'ru' ? 'участников' : 'participants';
        const winnerWord = lang === 'ru' ? 'Победитель' : 'Winner';
        const ticketWord = lang === 'ru' ? 'билет' : 'ticket';

        list.innerHTML = draws.map(function (d) {
            const date = d.date ? new Date(d.date).toLocaleDateString(
                lang === 'ru' ? 'ru-RU' : 'en-GB',
                { day: '2-digit', month: '2-digit', year: 'numeric' }
            ) : '';
            const blockLink = d.block
                ? '<a href="https://bscscan.com/block/' + d.block + '" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;text-underline-offset:3px;">#' + d.block + '</a>'
                : '';
            const verifyWord = lang === 'ru' ? 'Проверить' : 'Verify';
            const verifyLink = (d.block && d.participants)
                ? ' &middot; <a href="#verify" onclick="verifyFromHistory(' + d.block + ',' + d.participants + ');return false;" style="color:var(--gold);text-decoration:underline;text-underline-offset:3px;">' + verifyWord + ' ↗</a>'
                : '';
            return (
                '<div class="history-row">' +
                    '<div class="history-round">#' + d.round + '<span>' + date + '</span></div>' +
                    '<div class="history-mid">' + (d.participants ?? 0) + ' ' + membersWord +
                        ' &middot; ' + winnerWord + ': <b>' + (d.winner || '—') + '</b> (' + ticketWord + ' #' + d.ticket + ')' +
                        (blockLink ? ' &middot; ' + blockLink : '') + verifyLink +
                    '</div>' +
                    '<div class="history-prize">' + (d.prize != null ? d.prize.toFixed(2) : '0.00') + ' USDT</div>' +
                '</div>'
            );
        }).join('');
    } catch (error) {
        list.innerHTML = '<div class="history-empty">' +
            (lang === 'ru' ? 'История временно недоступна.' : 'History is temporarily unavailable.') +
            '</div>';
    }
}

async function loadLeaderboard() {
    const list = document.getElementById('leaderboard-list');
    if (!list) return;
    const lang = (document.documentElement.lang || 'en').slice(0, 2);
    const medals = ['🥇', '🥈', '🥉'];
    const winsWord = lang === 'ru' ? 'побед' : 'wins';

    try {
        const response = await fetch('https://cryptofortunabot.onrender.com/api/leaderboard');
        const rows = await response.json();

        if (!Array.isArray(rows) || rows.length === 0) {
            list.innerHTML = '<div class="leader-empty">' +
                (lang === 'ru' ? 'Розыгрышей пока не было.' : 'No draws have taken place yet.') +
                '</div>';
            return;
        }

        list.innerHTML = rows.map(function (r, i) {
            const rank = medals[i] || (i + 1) + '.';
            return (
                '<div class="leader-row">' +
                    '<div class="leader-rank">' + rank + '</div>' +
                    '<div class="leader-name">' + (r.winner || '—') +
                        '<div class="leader-wins">' + r.wins + ' ' + winsWord + '</div>' +
                    '</div>' +
                    '<div class="leader-total">' + (r.total != null ? r.total.toFixed(2) : '0.00') + ' USDT</div>' +
                '</div>'
            );
        }).join('');
    } catch (error) {
        list.innerHTML = '<div class="leader-empty">' +
            (lang === 'ru' ? 'Лидерборд временно недоступен.' : 'Leaderboard is temporarily unavailable.') +
            '</div>';
    }
}

async function runVerify() {
    const blockEl = document.getElementById('verify-block');
    const countEl = document.getElementById('verify-count');
    const resultEl = document.getElementById('verify-result');
    const btn = document.getElementById('verify-btn');
    if (!blockEl || !countEl || !resultEl) return;
    const lang = (document.documentElement.lang || 'en').slice(0, 2);

    const block = parseInt(blockEl.value, 10);
    const count = parseInt(countEl.value, 10);
    if (!block || !count || count < 1) {
        resultEl.className = 'verify-result error';
        resultEl.textContent = lang === 'ru'
            ? 'Укажи номер блока и число участников.'
            : 'Enter a block number and participant count.';
        return;
    }

    btn.disabled = true;
    resultEl.className = 'verify-result';
    resultEl.textContent = lang === 'ru' ? 'Проверяю блок в сети BSC…' : 'Checking the block on BSC…';

    try {
        const response = await fetch('https://cryptofortunabot.onrender.com/api/verify?block=' + block + '&count=' + count);
        const data = await response.json();
        if (!response.ok || data.error || data.detail) {
            throw new Error(data.detail || data.error || 'verify failed');
        }
        const hashShort = data.block_hash.slice(0, 24) + '…';
        resultEl.innerHTML =
            (lang === 'ru' ? 'Хэш блока: ' : 'Block hash: ') + '<code>' + hashShort + '</code>' +
            ' <a href="https://bscscan.com/block/' + data.block + '" target="_blank" rel="noopener" style="color:var(--gold);text-decoration:underline;">bscscan ↗</a><br>' +
            (lang === 'ru' ? 'Билет победителя: ' : 'Winning ticket: ') +
            '<span class="ticket">#' + data.winner_ticket + '</span>';
    } catch (error) {
        resultEl.className = 'verify-result error';
        resultEl.textContent = lang === 'ru'
            ? 'Не удалось проверить — блок ещё не добыт или сервис временно недоступен.'
            : "Couldn't verify — the block may not be mined yet, or the service is temporarily unavailable.";
    } finally {
        btn.disabled = false;
    }
}

function verifyFromHistory(block, count) {
    const blockEl = document.getElementById('verify-block');
    const countEl = document.getElementById('verify-count');
    if (!blockEl || !countEl) return;
    blockEl.value = block;
    countEl.value = count;
    document.getElementById('verify').scrollIntoView({ behavior: 'smooth', block: 'center' });
    runVerify();
}

loadStats();
loadHistory();
loadLeaderboard();
// Обновляем статистику каждые 30 секунд, историю и лидерборд — раз в минуту
setInterval(loadStats, 30000);
setInterval(loadHistory, 60000);
setInterval(loadLeaderboard, 60000);
