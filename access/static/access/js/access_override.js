document.querySelectorAll('.access-override-form').forEach((form) => {
    const radios = [...form.querySelectorAll('input[name="access_override"]')];
    form.querySelector('.override-save').hidden = true;

    radios.forEach((radio) => {
        radio.addEventListener('change', () => form.requestSubmit());
    });

    document.querySelectorAll('form[data-confirm]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            if (!window.confirm(form.dataset.confirm)) {
                event.preventDefault();
            }
        });
    });

    form.addEventListener('submit', (event) => {
        const selected = radios.find((radio) => radio.checked);
        if (selected.value === form.dataset.current) {
            event.preventDefault();
            return;
        }
        const description = selected.value === 'auto'
            ? 'Auto (use automatic authorization rules)'
            : `Manual ${selected.value === 'grant' ? 'Grant' : 'Reject'} (override automatic rules)`;
        if (!window.confirm(`Set access for ${form.dataset.member} to ${description}? Offline devices receive this change on their next sync.`)) {
            event.preventDefault();
            radios.find((radio) => radio.value === form.dataset.current).checked = true;
        }
    });
});
