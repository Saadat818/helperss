(function () {
    function readStorage(key) {
        try {
            return localStorage.getItem(key);
        } catch (e) {
            return null;
        }
    }

    function writeStorage(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (e) {
            // localStorage can be blocked in private modes; UI should still work.
        }
    }

    const storedTheme = readStorage('theme');
    if (storedTheme === 'dark') {
        document.body.classList.add('dark');
    }

    window.toggleTheme = function () {
        const isDark = document.body.classList.toggle('dark');
        writeStorage('theme', isDark ? 'dark' : 'light');
    };

    const sidebarStorageKey = 'helperSidebarCollapsed';

    function updateSidebarToggle(collapsed) {
        const toggle = document.querySelector('[data-sidebar-toggle]');
        if (!toggle) return;

        const label = collapsed ? 'Развернуть меню' : 'Свернуть меню';
        toggle.setAttribute('title', label);
        toggle.setAttribute('aria-label', label);
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }

    function setSidebarCollapsed(collapsed, persist) {
        document.body.classList.toggle('sidebar-collapsed', collapsed);
        document.body.classList.remove('sidebar-hover');
        updateSidebarToggle(collapsed);

        if (persist) {
            writeStorage(sidebarStorageKey, collapsed ? '1' : '0');
        }
    }

    function initSidebar() {
        const sidebar = document.querySelector('.sidebar');
        const toggle = document.querySelector('[data-sidebar-toggle]');
        if (!sidebar || !toggle) return;

        const collapsed = readStorage(sidebarStorageKey) === '1';
        setSidebarCollapsed(collapsed, false);

        toggle.addEventListener('click', function () {
            setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'), true);
        });

        sidebar.addEventListener('pointerover', function (event) {
            if (
                document.body.classList.contains('sidebar-collapsed') &&
                !event.target.closest('.sidebar-user, [data-sidebar-toggle]')
            ) {
                document.body.classList.add('sidebar-hover');
            }
        });

        sidebar.addEventListener('mouseleave', function () {
            document.body.classList.remove('sidebar-hover');
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSidebar);
    } else {
        initSidebar();
    }

    window.toggleSidebar = function () {
        setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'), true);
    };
})();
