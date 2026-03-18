let selectedPalladiumId = null;
let selectedfullName = null;
let pollMappingIntervalId = null;

// closed by default on page load
updateDBModalState("closed");

function updateDBModalState(state) {
    fetch("/set-modal-state", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: "{\"state\": \"" + state + "\"}"
    });
}

function openMappingModal(button) {

    const row = button.closest("tr");
    selectedPalladiumId = row.dataset.palladiumId;
    selectedfullName = row.dataset.fullName;

    console.log(selectedPalladiumId, selectedfullName);

    document.getElementById("pendingPalladium").innerText = selectedPalladiumId;
    document.getElementById("pendingfullName").innerText = selectedfullName;

    // Show pending badge
    document.getElementById("pendingBadge-" + selectedPalladiumId).classList.remove("d-none");

    updateDBModalState("open");

    // Start blinking the waiting text
    document.getElementById("pendingMa300").classList.add("blink");

    // Start polling for TempMapping updates
    pollTempMapping();

    new bootstrap.Modal(document.getElementById('mappingModal')).show();
}

function closeMappingModal() {

    updateDBModalState("closed");
    
    // Hide pending badge
    if(selectedPalladiumId) {
        document.getElementById("pendingBadge-" + selectedPalladiumId).classList.add("d-none");
    }
    // Stop polling
    clearTimeout(pollMappingIntervalId);
}

function pollTempMapping() {
    pollMappingIntervalId = setInterval(() => {
        fetch("/poll-temp-mapping")
        .then(res => res.json())
        .then(data => {
        const ma300Element = document.getElementById("pendingMa300");
        const newId = data.device_access_id || "Waiting for biometric/card input...";
        ma300Element.innerText = newId;
        
        // Stop blinking once we have a real ID (not the waiting message)
        if (data.device_access_id) {
            ma300Element.classList.remove("blink");
        }
        });   
    }, 2000);
}

function confirmMapping() {
    const device_access_id = document.getElementById("pendingMa300").innerText;
    fetch("/confirm-mapping", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: "{\"device_access_id\": \"" + device_access_id + "\", \"account_user_id\": \"" + selectedPalladiumId + "\"}"
    }).then(res => res.json()).then(data => {
        alert("Connection saved!");
        closeMappingModal();
        location.reload();
    });
}