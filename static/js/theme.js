// Применяем тему ДО рендера — избегаем мигания
(function () {
    if (localStorage.getItem('theme') === 'light') {
        document.documentElement.classList.add('light');
    }
})();

document.addEventListener('DOMContentLoaded', function () {
    // Синхронизируем body
    if (document.documentElement.classList.contains('light')) {
        document.body.classList.add('light');
    }

    // Обновляем иконку кнопки если она есть
    var btn = document.getElementById('themeToggle');
    if (btn && document.body.classList.contains('light')) {
        btn.textContent = '☀️';
    }
});

function toggleTheme() {
    var isLight = document.body.classList.toggle('light');
    document.documentElement.classList.toggle('light', isLight);
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
    var btn = document.getElementById('themeToggle');
    if (btn) btn.textContent = isLight ? '☀️' : '🌙';
}
