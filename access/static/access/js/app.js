let selectedAccountingId = null;
let selectedfullName = null;
let pollMappingIntervalId = null;
let modalMode = 'map'; // 'map' or 'unmap'
let mappingModalInstance = null;

document.addEventListener("DOMContentLoaded", function() {
    mappingModalInstance = new bootstrap.Modal(document.getElementById('mappingModal'));

    // closed by default on page load
    updateDBModalState("closed");

    // auto dismiss settings update alerts after 5 seconds
    const alertElements = document.querySelectorAll('.alert');
    alertElements.forEach(alertEl => {
        setTimeout(() => {
            const alert = bootstrap.Alert.getOrCreateInstance(alertEl);
            alert.close();
        }, 5000);
    });
});

function showAlert(message, type = 'success', placement = 'body') {

    if (placement == 'modal') {
        let container = document.getElementById('modalAlert');
        container.querySelector('div').innerText = message; 
        container.classList.remove('d-none');
        return;
    }

    // set alert display conditions
    message += ' Page will AUTOMATICALLY REFRESH in 5 seconds.';
    location.href = "#";
    closeMappingModal();

    // create alert
    const container = document.getElementById('alert-container');
    if (!container) return;

    const wrapper = document.createElement('div');
    wrapper.innerHTML = `
        <div class="alert alert-${type} alert-dismissible fade show" role="alert">
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        </div>
    `;

    container.appendChild(wrapper);
    setTimeout(() => {
        const alertEl = wrapper.querySelector('.alert');
        if (alertEl) {
            const alert = bootstrap.Alert.getOrCreateInstance(alertEl);
            alert.close();
        }
    }, 5000);
}

function updateDBModalState(state) {
    fetch("api/modal-state", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ state })
    }).then(res => {
        if (!res.ok) {
            console.error("Failed to update modal state in DB");
        }    
    }).catch(err => {
        console.error("Error updating modal state in DB:", err);
    });
}

function openMappingModal(button) {
    modalMode = 'map';

    const row = button.closest("tr");
    selectedAccountingId = row.dataset.accountingId;
    selectedfullName = row.dataset.fullName;

    document.getElementById("pendingAccounting").innerText = selectedAccountingId;
    document.getElementById("pendingfullName").innerText = selectedfullName;

    let mappingModalTitle = document.getElementById('mappingModalTitle');
    mappingModalTitle.innerText = 'Connection Pending';
    mappingModalTitle.classList.add('text-light');
    let MappingModalHeader = document.querySelector('.modal-header');
    MappingModalHeader.classList.remove('bg-warning');
    MappingModalHeader.classList.add('bg-success');
    document.getElementById('pendingAccessDeviceRow').classList.remove('d-none');
    const confirmBtn = document.getElementById('modalConfirmBtn');
    confirmBtn.innerHTML = 'Confirm <i class="bi bi-plugin ms-1"></i>';
    confirmBtn.classList.remove('btn-danger');
    confirmBtn.classList.add('btn-success');

    // Show pending badge
    let pendingBage = document.getElementById("pendingBadge-" + selectedAccountingId);
    pendingBage.innerText = "Pending Connection...";
    pendingBage.classList.remove("d-none");

    updateDBModalState("open");

    // Start blinking the waiting text
    document.getElementById("pendingAccessDevice").classList.add("blink");

    // Start polling for TempMapping updates
    pollTempMapping();

    mappingModalInstance.show();
}

function openUnmapModal(button) {
    modalMode = 'unmap';

    const row = button.closest("tr");
    selectedAccountingId = row.dataset.accountingId;
    selectedfullName = row.dataset.fullName;

    document.getElementById("pendingAccounting").innerText = selectedAccountingId;
    document.getElementById("pendingfullName").innerText = selectedfullName;
    document.getElementById("pendingAccessDevice").innerText = "---";
    document.getElementById("pendingAccessDevice").classList.remove("blink");

    let mappingModalTitle = document.getElementById('mappingModalTitle');
    mappingModalTitle.innerText = 'Confirm Disconnection';
    mappingModalTitle.classList.remove('text-light');
    mappingModalTitle.classList.add('text-dark');
    let MappingModalHeader = document.querySelector('.modal-header');
    MappingModalHeader.classList.remove('bg-success');
    MappingModalHeader.classList.add('bg-warning');
    document.getElementById('pendingAccessDeviceRow').classList.add('d-none');
    const confirmBtn = document.getElementById('modalConfirmBtn');
    confirmBtn.innerHTML = 'Disconnect <i class="bi bi-x-circle ms-1"></i>';
    confirmBtn.classList.remove('btn-success');
    confirmBtn.classList.add('btn-danger');

    // Show pending badge
    let pendingBage = document.getElementById("pendingBadge-" + selectedAccountingId);
    pendingBage.innerText = "Pending Disconnection...";
    pendingBage.classList.remove("d-none");

    // No polling needed; just show modal
    mappingModalInstance.show();
}

function closeMappingModal() {

    mappingModalInstance.hide();

    updateDBModalState("closed");
    
    // Hide pending badge
    if(selectedAccountingId) {
        document.getElementById("pendingBadge-" + selectedAccountingId).classList.add("d-none");
    }

    // Stop polling
    clearInterval(pollMappingIntervalId);

    // Clear error messages
    let container = document.getElementById('modalAlert');
    container.querySelector('div').innerText = ""; 
    container.classList.add('d-none');

    // clear pending info
    document.getElementById("pendingAccounting").innerText = "";
    document.getElementById("pendingfullName").innerText = "";
    document.getElementById("pendingAccessDevice").innerText = "";
}

function pollTempMapping() {
    pollMappingIntervalId = setInterval(() => {
        fetch("api/mappings/pending")
        .then(res => {
            if (!res.ok) {
                throw new Error("Failed to fetch pending mapping");
            }   
            return res.json();
        }).then(data => {
            const ma300Element = document.getElementById("pendingAccessDevice");
            const newId = data.device_access_id || "Waiting for biometric/card input...";
            ma300Element.innerText = newId;
            
            // Stop blinking once we have a real ID (not the waiting message)
            if (data.device_access_id) {
                ma300Element.classList.remove("blink");
            }
        }).catch(err => {
            console.error("Error fetching pending mapping:", err);
        });   
    }, 2000);
}

function confirmMapping() {
    if (modalMode === 'unmap') {
        // Remove mapping flow
        return removeMapping();
    }

    // Create mapping flow
    const device_access_id = document.getElementById("pendingAccessDevice").innerText;
    if(device_access_id === "Waiting for biometric/card input..."){
        showAlert("Please scan fingerprint/card to get the device access ID before confirming.", 'danger', 'modal');
        return;
    }

    fetch("api/mappings", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ device_access_id, account_user_id: selectedAccountingId })
    }).then(res => {
        if (!res.ok) {
            return res.json().then(errData => {
                throw new Error(errData.error || "Unknown error");
            });
        }
        return res.json();
    }).then(data => {
        closeMappingModal();
        showAlert("User connection successfully saved!", 'success');
        setTimeout(() => {location.reload();}, 5000);
    }).catch(err => {
        showAlert("Error saving connection: " + err.message, 'danger', 'modal');
    });
}

function syncUsers() {
    const syncButton = document.getElementById('syncUsersBtn');
    if (syncButton) {
        syncButton.disabled = true;
        syncButton.innerHTML = 'Syncing... <i class="bi bi-arrow-repeat ms-1"></i>';
    }

    fetch('api/mappings/update', {
        method: 'POST',
    }).then(async (res) => {
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || 'Unable to sync users.');
        }
        return data;
    }).then(() => {
        showAlert('Users synced successfully!', 'success');
        setTimeout(() => { location.reload(); }, 5000);
    }).catch((err) => {
        showAlert(`Error syncing users: ${err.message}`, 'danger');
    }).finally(() => {
        if (syncButton) {
            syncButton.disabled = false;
            syncButton.innerHTML = 'Sync Users <i class="bi bi-arrow-repeat ms-1"></i>';
        }
    });
}

function removeMapping() {
    fetch("api/mappings/" + selectedAccountingId, {
        method: "DELETE",
    }).then(async res => {
        if (!res.ok) {
            const errData = await res.json();
            throw new Error(errData.error || "Unknown error");
        }
        return true;
    }).then(data => {
        closeMappingModal();
        showAlert("User disconnected successfully.", 'success');
        setTimeout(() => {location.reload();}, 5000);
        
    }).catch(err => {
        showAlert("Error disconnecting user: " + err.message, 'danger', 'modal');
    });
}