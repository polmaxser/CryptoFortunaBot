async function loadStats() {
    const participantsEl = document.getElementById('live-participants');
    const limitEl = document.getElementById('live-limit');
    const bankEl = document.getElementById('live-bank');
    const paidEl = document.getElementById('live-paid');
    const drawsEl = document.getElementById('live-draws');

    try {
        const response = await fetch('https://cryptofortunabot.onrender.com/stats');
        const data = await response.json();

        if (participantsEl) participantsEl.innerText = data.current_participants ?? 0;
        if (limitEl) limitEl.innerText = data.current_limit ?? '—';
        if (bankEl) bankEl.innerText = (data.current_bank ?? 0) + ' USDT';
        if (paidEl) paidEl.innerText = Math.round((data.total_commission || 0) * 9) + ' USDT';
        if (drawsEl) drawsEl.innerText = data.total_draws ?? 0;
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
            return (
                '<div class="history-row">' +
                    '<div class="history-round">#' + d.round + '<span>' + date + '</span></div>' +
                    '<div class="history-mid">' + (d.participants ?? 0) + ' ' + membersWord +
                        ' &middot; ' + winnerWord + ': <b>' + (d.winner || '—') + '</b> (' + ticketWord + ' #' + d.ticket + ')' +
                        (blockLink ? ' &middot; ' + blockLink : '') +
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

loadStats();
loadHistory();
loadLeaderboard();
// Обновляем статистику каждые 30 секунд, историю и лидерборд — раз в минуту
setInterval(loadStats, 30000);
setInterval(loadHistory, 60000);
setInterval(loadLeaderboard, 60000);
