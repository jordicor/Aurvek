// edit_profile.js

let phoneInputJS;
let audioPlayer = null;
let alterEgoModal;
let phoneChallengeId = null;
let challengePhoneNumber = null;
let phoneChallengeApproved = false;

function resetPhoneVerificationState() {
    phoneChallengeId = null;
    challengePhoneNumber = null;
    phoneChallengeApproved = false;
    const phoneInput = document.getElementById('phone');
    const verificationInput = document.getElementById('verificationCode');
    const verificationIdInput = document.getElementById('phoneVerificationId');
    const verificationContainer = document.getElementById('verificationCodeContainer');
    if (phoneInput) delete phoneInput.dataset.verified;
    if (verificationInput) {
        verificationInput.value = '';
        verificationInput.disabled = false;
    }
    if (verificationIdInput) verificationIdInput.value = '';
    if (verificationContainer) verificationContainer.style.display = 'none';
}

document.addEventListener('DOMContentLoaded', function() {
    initAfterLoginPreference();
    initPhoneInput();
    initializeAlterEgoState();
    loadVoices();
    loadAlterEgos(currentAlterEgoId);
    setupEventListeners();
    initializeProfileHandlers();

    alterEgoModal = new bootstrap.Modal(document.getElementById('alterEgoModal'));

    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl)
    });

    const deleteProfilePictureButton = document.getElementById('deleteProfilePictureButton');
    if (deleteProfilePictureButton) {
        deleteProfilePictureButton.addEventListener('click', deleteProfilePicture);
    }

    initializeAlterEgoHandlers();

    // FormGuard -- snapshot with excluded async-loaded fields
    FormGuard.watch('#editProfileForm', {
        exclude: ['sample_voice_id', 'alter_ego_id']
    });
});

// Initialize when DOM is ready

function initPhoneInput() {
    const phoneInputField = document.getElementById('phone');
    phoneInputJS = window.intlTelInput(phoneInputField, {
        initialCountry: "auto",
        separateDialCode: true,
        utilsScript: "https://cdnjs.cloudflare.com/ajax/libs/intl-tel-input/17.0.13/js/utils.js",
        geoIpLookup: function(success, failure) {
            secureFetch('/api/get-ip-info')
                .then(function(response) {
                    if (!response) return null;
                    if (response.ok) return response.json();
                    throw new Error('Failed to fetch IP info');
                })
                .then(function(ipinfo) {
                    if (!ipinfo) return;
                    success(ipinfo.country);
                })
                .catch(function() {
                    success("us");
                });
        },
    });
}

function initializeAlterEgoState() {
    const useRealProfile = document.getElementById('useRealProfile');
    const useAlterEgo = document.getElementById('useAlterEgo');
    const alterEgoSelection = document.getElementById('alterEgoSelection');

    if (currentAlterEgoId && currentAlterEgoId !== "0") {
        useAlterEgo.checked = true;
        alterEgoSelection.style.display = 'block';
    } else {
        useRealProfile.checked = true;
        alterEgoSelection.style.display = 'none';
    }

    useRealProfile.addEventListener('change', toggleAlterEgoSelection);
    useAlterEgo.addEventListener('change', toggleAlterEgoSelection);
}

function toggleAlterEgoSelection() {
    const useAlterEgo = document.getElementById('useAlterEgo');
    const alterEgoSelection = document.getElementById('alterEgoSelection');
    alterEgoSelection.style.display = useAlterEgo.checked ? 'block' : 'none';
}

function loadVoices() {
    const voiceSelect = document.getElementById('voice');
    secureFetch('/api/voices')
        .then(response => {
            if (!response) return null;
            return response.json();
        })
        .then(voices => {
            if (!voices) return;
            voiceSelect.innerHTML = '<option value="">Default Voice</option>';
            voices.forEach(voice => {
                const option = document.createElement('option');
                option.value = voice.id;
                option.textContent = voice.name;
                voiceSelect.appendChild(option);
            });
            if (currentUserVoiceId && currentUserVoiceId !== "None") {
                voiceSelect.value = currentUserVoiceId;
            }
            addPlayButton();
            // Re-snapshot form after voice hydration
            FormGuard.markClean('#editProfileForm');
        })
        .catch(error => console.error('Error loading voices:', error));
}

function addPlayButton() {
    const playButtonContainer = document.getElementById('playButtonContainer');
    playButtonContainer.innerHTML = '';

    const categorySelect = document.createElement('select');
    categorySelect.className = 'form-select form-select-sm me-2';
    categorySelect.id = 'sampleCategory';
    categorySelect.style.display = 'inline-block';
    categorySelect.style.width = 'auto';

    const categories = [
        "Children and Basic Education",
        "Finance and Business",
        "Relaxation and Meditation",
        "Casual Conversation",
        "Drama and Emotional Narration",
        "Storytelling",
        "Advertising and Announcements",
        "Science and Technology",
        "Education and Advanced Training",
        "Corporate Environments",
        "Mystery and Suspense",
        "Sports and Energy"
    ];

    categories.forEach((category, index) => {
        const option = document.createElement('option');
        option.value = index;
        option.textContent = category;
        categorySelect.appendChild(option);
    });

    const playButton = document.createElement('button');
    playButton.textContent = '▶️ Play Sample';
    playButton.className = 'btn btn-sm btn-outline-secondary play-voice';
    playButton.id = 'playVoiceButton';
    playButton.style.display = document.getElementById('voice').value ? 'inline-block' : 'none';
    playButton.addEventListener('click', function(e) {
        e.preventDefault();
        playVoiceSample(document.getElementById('voice').value, categorySelect.value);
    });

    playButtonContainer.appendChild(categorySelect);
    playButtonContainer.appendChild(playButton);

    document.getElementById('voice').addEventListener('change', function() {
        playButton.style.display = this.value ? 'inline-block' : 'none';
        categorySelect.style.display = this.value ? 'inline-block' : 'none';
    });
}

function playVoiceSample(voiceId, categoryId) {
    if (!voiceId) return;

    if (audioPlayer) {
        audioPlayer.pause();
        audioPlayer.currentTime = 0;
        audioPlayer = null;
        const playButton = document.getElementById('playVoiceButton');
        playButton.textContent = '▶️ Play Sample';
        return;
    }

    const playButton = document.getElementById('playVoiceButton');
    playButton.textContent = '🔄 Loading...';
    playButton.disabled = true;

    secureFetch(`/api/voice-sample/${voiceId}?category=${categoryId}`)
        .then(response => {
            if (!response) return null;
            return response.blob();
        })
        .then(blob => {
            if (!blob) return;
            const url = URL.createObjectURL(blob);
            audioPlayer = new Audio(url);

            audioPlayer.onended = function() {
                playButton.textContent = '▶️ Play Sample';
                playButton.disabled = false;
                audioPlayer = null;
            };

            audioPlayer.play();
            playButton.textContent = '⏹️ Stop Sample';
            playButton.disabled = false;

            playButton.onclick = function() {
                if (audioPlayer) {
                    audioPlayer.pause();
                    audioPlayer.currentTime = 0;
                    audioPlayer = null;
                    playButton.textContent = '▶️ Play Sample';
                }
            };
        })
        .catch(error => {
            console.error('Error playing voice sample:', error);
            playButton.textContent = '▶️ Play Sample';
            playButton.disabled = false;
        });
}

function setupEventListeners() {
    const phoneInput = document.getElementById('phone');
    const sendCodeButton = document.getElementById('sendCodeButton');
    const verificationCodeContainer = document.getElementById('verificationCodeContainer');
    const verificationCodeInput = document.getElementById('verificationCode');
    const phoneVerificationIdInput = document.getElementById('phoneVerificationId');
    const editProfileForm = document.getElementById('editProfileForm');

    if (phoneInput.value && phoneInput.dataset.phoneVerified === 'false') {
        sendCodeButton.style.display = 'block';
    }

    const verifyCodeButton = document.createElement('button');
    verifyCodeButton.textContent = 'Verify Code';
    verifyCodeButton.className = 'btn btn-primary mt-2';
    verifyCodeButton.style.display = 'none';
    verificationCodeContainer.appendChild(verifyCodeButton);

    verificationCodeInput.addEventListener('input', function() {
        verifyCodeButton.style.display = this.value ? 'block' : 'none';
    });

    verifyCodeButton.addEventListener('click', async function(e) {
        e.preventDefault();
        const phoneNumber = phoneInputJS.getNumber(intlTelInputUtils.numberFormat.E164);
        const code = verificationCodeInput.value;
        if (!phoneChallengeId || challengePhoneNumber !== phoneNumber) {
            NotificationModal.error('Error', 'Request a new code for this phone number.');
            return;
        }

        try {
            const response = await secureFetch('/api/verify-code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    challenge_id: phoneChallengeId,
                    phone: phoneNumber,
                    purpose: 'profile_phone_change',
                    code: code
                }),
            });
            if (!response) return; // Session expired
            const result = await response.json();
            if (result.status === 'approved') {
                NotificationModal.success('Success', 'Phone number verified successfully!');
                verifyCodeButton.style.display = 'none';
                verificationCodeInput.disabled = true;
                phoneInput.dataset.verified = 'true';
                phoneChallengeApproved = true;
                phoneVerificationIdInput.value = phoneChallengeId;
            } else {
                NotificationModal.error('Error', result.detail || 'Verification failed. Please check the code and try again.');
                verificationCodeInput.value = '';
            }
        } catch (error) {
            console.error('Error:', error);
            NotificationModal.error('Error', 'An error occurred while verifying the code. Please try again.');
        }
    });

    phoneInput.addEventListener('input', function() {
        resetPhoneVerificationState();
        if (this.value) {
            sendCodeButton.style.display = 'block';
        } else {
            sendCodeButton.style.display = 'none';
            verificationCodeContainer.style.display = 'none';
        }
    });

    sendCodeButton.addEventListener('click', async function() {
        const phoneNumber = phoneInputJS.getNumber(intlTelInputUtils.numberFormat.E164);

        try {
            const checkResponse = await secureFetch('/api/check-phone-number', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ phone: phoneNumber }),
            });
            if (!checkResponse) return; // Session expired
            const checkResult = await checkResponse.json();

            if (checkResult.exists) {
                NotificationModal.error('Error', 'This phone number is already in use. Please use a different number.');
                return;
            }

            const response = await secureFetch('/api/send-verification-code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    phone: phoneNumber,
                    purpose: 'profile_phone_change'
                }),
            });
            if (!response) return; // Session expired
            const result = await response.json();
            if (response.ok) {
                if (result.status === 'pending') {
                    phoneChallengeId = result.challenge_id;
                    challengePhoneNumber = phoneNumber;
                    phoneChallengeApproved = false;
                    phoneVerificationIdInput.value = '';
                    verificationCodeInput.value = '';
                    verificationCodeInput.disabled = false;
                    verificationCodeContainer.style.display = 'block';
                    NotificationModal.success('Success', 'Verification code sent successfully!');
                } else {
                    NotificationModal.error('Error', `Unexpected status: ${result.status}`);
                }
            } else {
                NotificationModal.error('Error', `Error sending verification code: ${result.detail}`);
            }
        } catch (error) {
            console.error('Error:', error);
            NotificationModal.error('Error', 'An unexpected error occurred. Please try again.');
        }
    });

    editProfileForm.addEventListener('submit', handleFormSubmit);
}

async function handleFormSubmit(event) {
    event.preventDefault();
    const formData = new FormData(event.target);
    
    const fullPhoneNumber = getFullPhoneNumber();
    formData.set('phone_number', fullPhoneNumber);

    const phoneInput = document.getElementById('phone');
    const phoneNeedsVerification = Boolean(fullPhoneNumber) && (
        fullPhoneNumber !== originalPhoneNumber
        || phoneInput.dataset.phoneVerified !== 'true'
    );

    if (phoneNeedsVerification) {
        try {
            const checkResponse = await secureFetch('/api/check-phone-number', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ phone: fullPhoneNumber }),
            });
            if (!checkResponse) return; // Session expired
            const checkResult = await checkResponse.json();

            if (checkResult.exists) {
                NotificationModal.error('Error', 'This phone number is already in use. Please use a different number.');
                return;
            }

            if (
                phoneInput.dataset.verified !== 'true'
                || !phoneChallengeApproved
                || challengePhoneNumber !== fullPhoneNumber
                || !phoneChallengeId
            ) {
                NotificationModal.error('Error', 'Please verify your new phone number before submitting.');
                return;
            }
            formData.set('phone_verification_id', phoneChallengeId);
        } catch (error) {
            console.error('Error:', error);
            NotificationModal.error('Error', 'An error occurred while checking the phone number.');
            return;
        }
    }

    const useAlterEgo = document.getElementById('useAlterEgo').checked;
    const alterEgoSelect = document.getElementById('alterEgo');
    if (useAlterEgo && alterEgoSelect.value !== "0") {
        formData.set('alter_ego_id', alterEgoSelect.value);
    } else {
        formData.set('alter_ego_id', "0");
    }

    secureFetch('/api/edit-profile', {
        method: 'POST',
        body: formData,
        headers: {
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (!response) return null;
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            currentUserVoiceId = formData.get('sample_voice_id');
            originalPhoneNumber = fullPhoneNumber;
            if (phoneChallengeApproved && challengePhoneNumber === fullPhoneNumber) {
                document.getElementById('phone').dataset.phoneVerified = 'true';
            }
            currentAlterEgoId = formData.get('alter_ego_id');
            if (data.reauthenticate) {
                FormGuard.markClean('#editProfileForm');
                NotificationModal.success('Success', 'Phone number updated. Please sign in again.');
                setTimeout(() => {
                    window.location.href = '/login';
                }, 1200);
                return;
            }
            // Save after-login and web search preferences alongside profile
            Promise.all([saveAfterLoginPreference(), saveWebSearchSettings()])
                .then(function() {
                    FormGuard.markClean('#editProfileForm');
                    NotificationModal.success('Success', 'Profile updated successfully');
                })
                .catch(function(err) {
                    NotificationModal.error('Error', 'Some preferences failed to save: ' + err.message);
                });
        } else {
            NotificationModal.error('Error', 'Error updating profile: ' + data.message);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        NotificationModal.error('Error', 'An error occurred while updating the profile.');
    });
}

function getFullPhoneNumber() {
    return phoneInputJS.getNumber(intlTelInputUtils.numberFormat.E164);
}

// Functions related to alter-ego management
function loadAlterEgos(currentAlterEgoId) {
    secureFetch('/api/get-alter-egos', {
        method: 'GET',
        headers: {
            'Content-Type': 'application/json',
        }
    }).then(response => {
        if (!response) return null;
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            const alterEgoSelect = document.getElementById('alterEgo');
            alterEgoSelect.innerHTML = '<option value="0">Select an alter-ego</option>';
            data.alterEgos.forEach(alterEgo => {
                let option = document.createElement('option');
                option.value = alterEgo.id;
                option.text = alterEgo.name;
                alterEgoSelect.appendChild(option);
            });

            if (currentAlterEgoId && currentAlterEgoId !== "0") {
                alterEgoSelect.value = currentAlterEgoId;
                showAlterEgoDetails(currentAlterEgoId);
            } else {
                document.getElementById('alterEgoDetails').innerHTML = '';
            }
            // Re-snapshot form after alter-ego hydration
            FormGuard.markClean('#editProfileForm');
        } else {
            console.error('Error loading alter-egos:', data.message);
        }
    })
    .catch(error => {
        console.error('Error fetching alter-egos:', error);
    });
}

function showAlterEgoDetails(alterEgoId) {
    const alterEgoDetails = document.getElementById('alterEgoDetails');

    if (!alterEgoId || alterEgoId === "0" || alterEgoId === "") {
        alterEgoDetails.innerHTML = '';
        return;
    }

    secureFetch(`/api/get-alter-ego-details/${alterEgoId}`, {
        method: 'GET',
        headers: {
            'Content-Type': 'application/json',
        }
    }).then(response => {
        if (!response) return null;
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            const profilePicture = data.alterEgo.profilePicture
                ? `<img src="${data.alterEgo.profilePicture}" alt="Alter-Ego Profile Picture" style="width: 100px; height: 100px; object-fit: cover; border-radius: 50%;">`
                : `<div style="width: 100px; height: 100px; background-color: #f0f0f0; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 2em;">${data.alterEgo.name.charAt(0).toUpperCase()}</div>`;

            alterEgoDetails.innerHTML = `
                <div class="alter-ego-container">
                    <div class="alter-ego-header">
                        ${profilePicture}
                        <div>
                            <h3>${data.alterEgo.name}</h3>
                            <p>${data.alterEgo.description || 'No description available'}</p>
                        </div>
                    </div>
                    <div class="btn-group">
                        <button type="button" class="btn btn-primary" id="editAlterEgoButton">Edit</button>
                        <button type="button" class="btn btn-danger" id="deleteAlterEgoButton">Delete</button>
                    </div>
                </div>
            `;

            document.getElementById('editAlterEgoButton').addEventListener('click', function() {
                editAlterEgo(alterEgoId);
            });
            document.getElementById('deleteAlterEgoButton').addEventListener('click', function() {
                deleteAlterEgo(alterEgoId);
            });
        } else {
            console.error('Error loading alter-ego details:', data);
            alterEgoDetails.innerHTML = '<p>Error loading alter-ego details</p>';
        }
    })
    .catch(error => {
        console.error('Error fetching alter-ego details:', error);
        alterEgoDetails.innerHTML = '<p>An error occurred while loading alter-ego details</p>';
    });
}

function editAlterEgo(alterEgoId) {
    secureFetch(`/api/get-alter-ego-details/${alterEgoId}`, {
        method: 'GET',
        headers: {
            'Content-Type': 'application/json',
        }
    }).then(response => {
        if (!response) return null;
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            document.getElementById('alterEgoId').value = alterEgoId;
            document.getElementById('alterEgoName').value = data.alterEgo.name;
            document.getElementById('alterEgoDescription').value = data.alterEgo.description || '';

            const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');
            if (data.alterEgo.profilePicture) {
                alterEgoPictureContainer.innerHTML = `
                    <img src="${data.alterEgo.profilePicture}" alt="Alter-Ego Profile Picture" id="currentAlterEgoPicture">
                    <div class="avatar-icons">
                        <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                        <span class="avatar-icon delete"><i class="fas fa-trash"></i></span>
                    </div>
                `;
            } else {
                const initial = data.alterEgo.name.charAt(0).toUpperCase();
                alterEgoPictureContainer.innerHTML = `
                    <span class="avatar-initial" id="defaultAlterEgoInitial">${initial}</span>
                    <div class="avatar-icons">
                        <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                    </div>
                `;
            }

            document.getElementById('previewAlterContainer').classList.add('hidden');
            alterEgoPictureContainer.classList.remove('hidden');

            alterEgoModal.show();
        } else {
            console.error('Error loading alter-ego details:', data);
            NotificationModal.error('Error', 'Error loading alter-ego details for editing');
        }
    })
    .catch(error => {
        console.error('Error fetching alter-ego details:', error);
        NotificationModal.error('Error', 'An error occurred while loading alter-ego details');
    });
}

function deleteAlterEgo(alterEgoId) {
    NotificationModal.confirm(
        'Delete Alter Ego',
        'Are you sure you want to delete this alter-ego?',
        () => {
            secureFetch(`/api/delete-alter-ego/${alterEgoId}`, {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                }
            }).then(response => {
                if (!response) return null;
                return response.json();
            })
            .then(data => {
                if (!data) return;
                if (data.success) {
                    NotificationModal.success('Success', 'Alter-ego deleted successfully');
                    loadAlterEgos();
                    const alterEgoDetails = document.getElementById('alterEgoDetails');
                    if (alterEgoDetails) {
                        alterEgoDetails.innerHTML = '';
                    }
                    const alterEgoSelect = document.getElementById('alterEgo');
                    if (alterEgoSelect) {
                        alterEgoSelect.value = '';
                    }
                } else {
                    throw new Error(data.message || 'Error deleting alter-ego');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                NotificationModal.error('Error', 'An error occurred while deleting the alter-ego');
            });
        },
        null,
        { type: 'error', confirmText: 'Delete' }
    );
}

function deleteAlterEgoPicture() {
    NotificationModal.confirm(
        'Delete Alter Ego Picture',
        'Are you sure you want to delete this alter-ego picture?',
        () => {
            const alterEgoId = document.getElementById('alterEgoId').value;
            if (alterEgoId) {
                secureFetch(`/api/delete-alter-ego-picture/${alterEgoId}`, {
                    method: 'DELETE'
                })
                .then(response => {
                    if (!response) return null;
                    return response.json();
                })
                .then(data => {
                    if (!data) return;
                    if (data.success) {
                        updateAlterEgoPictureUI(alterEgoId);
                        NotificationModal.success('Success', 'Alter-ego picture deleted successfully');
                    } else {
                        NotificationModal.error('Error', 'Error deleting alter-ego picture');
                    }
                })
                .catch(error => {
                    console.error('Error deleting alter-ego picture:', error);
                    NotificationModal.error('Error', 'An error occurred while deleting the alter-ego picture');
                });
            } else {
                updateAlterEgoPictureUI();
            }
        },
        null,
        { type: 'error', confirmText: 'Delete' }
    );
}

function updateAlterEgoPictureUI(alterEgoId) {
    const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');
    const alterEgoName = document.getElementById('alterEgoName').value;
    alterEgoPictureContainer.innerHTML = `
        <span class="avatar-initial" id="defaultAlterEgoInitial">${alterEgoName.charAt(0).toUpperCase()}</span>
        <div class="avatar-icons">
            <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
        </div>
    `;

    if (alterEgoId) {
        const alterEgoDetails = document.getElementById('alterEgoDetails');
        if (alterEgoDetails) {
            const profilePicture = `<div style="width: 100px; height: 100px; background-color: #f0f0f0; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 2em;">${alterEgoName.charAt(0).toUpperCase()}</div>`;
            const headerElement = alterEgoDetails.querySelector('.alter-ego-header');
            if (headerElement) {
                headerElement.innerHTML = `
                    ${profilePicture}
                    <div>
                        <h3>${alterEgoName}</h3>
                        <p>${document.getElementById('alterEgoDescription').value || 'No description available'}</p>
                    </div>
                `;
            }
        }
    }

    const fileInput = document.getElementById('alterEgoProfilePicture');
    if (fileInput) {
        fileInput.value = '';
    }
}

function saveAlterEgo() {
    const formData = new FormData(document.getElementById('alterEgoForm'));
    const alterEgoId = formData.get('id');
    const url = alterEgoId ? `/api/update-alter-ego/${alterEgoId}` : '/api/create-alter-ego';
    const method = alterEgoId ? 'PUT' : 'POST';

    const fileInput = document.getElementById('alterEgoProfilePicture');
    const previewImage = document.getElementById('previewAlterEgoImage');
    
    if (fileInput.files.length > 0) {
        formData.set('profile_picture', fileInput.files[0]);
    } else if (previewImage.src && !previewImage.src.includes('data:image')) {
        formData.set('keep_existing_image', 'true');
    } else {
        formData.delete('profile_picture');
    }

    sendSaveRequest(url, method, formData);
}

function sendSaveRequest(url, method, formData) {
    secureFetch(url, {
        method: method,
        body: formData
    })
    .then(response => {
        if (!response) return null;
        if (!response.ok) {
            return response.json().then(err => { throw err; });
        }
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            NotificationModal.success('Success', 'Alter-ego saved successfully');
            alterEgoModal.hide();
            loadAlterEgos();
        } else {
            throw new Error(data.message || 'Error saving alter-ego');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        NotificationModal.error('Error', error.message || 'An error occurred while saving the alter-ego');
    });
}

function deleteProfilePicture() {
    NotificationModal.confirm(
        'Delete Profile Picture',
        'Are you sure you want to delete your profile picture?',
        () => {
            secureFetch('/api/delete-profile-picture', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ user_id: currentUserId })
            }).then(response => {
                if (!response) return null;
                return response.json();
            })
            .then(data => {
                if (!data) return;
                if (data.success) {
                    const profilePictureContainer = document.getElementById('profilePictureContainer');
                    profilePictureContainer.innerHTML = `
                        <span class="avatar-initial" id="defaultProfileInitial">${document.getElementById('username').value.charAt(0).toUpperCase()}</span>
                        <div class="avatar-icons">
                            <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                        </div>
                    `;
                    NotificationModal.success('Success', 'Profile picture deleted successfully');
                } else {
                    NotificationModal.error('Error', 'Error deleting profile picture');
                }
            });
        },
        null,
        { type: 'error', confirmText: 'Delete' }
    );
}

function cancelAlterEgoPictureChange() {
    const previewContainer = document.getElementById('previewAlterContainer');
    const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');

    previewContainer.classList.add('hidden');
    alterEgoPictureContainer.classList.remove('hidden');

    document.getElementById('alterEgoProfilePicture').value = '';
}

function initializeProfileHandlers() {
    const profilePictureContainer = document.getElementById('profilePictureContainer');
    const previewContainer = document.getElementById('previewContainer');
    const profilePictureInput = document.getElementById('profilePicture');

    if (profilePictureContainer) {
        const editIcon = profilePictureContainer.querySelector('.avatar-icon.edit');
        const deleteIcon = profilePictureContainer.querySelector('.avatar-icon.delete');

        if (editIcon) {
            editIcon.addEventListener('click', function() {
                profilePictureInput.click();
            });
        }

        if (deleteIcon) {
            deleteIcon.addEventListener('click', deleteProfilePicture);
        }
    }

    if (profilePictureInput) {
        profilePictureInput.addEventListener('change', function(e) {
            if (e.target.files && e.target.files[0]) {
                let reader = new FileReader();
                reader.onload = function(event) {
                    previewContainer.classList.remove('hidden');
                    const previewImage = previewContainer.querySelector('#previewImage');
                    if (previewImage) {
                        previewImage.src = event.target.result;
                    }
                    profilePictureContainer.classList.add('hidden');
                }
                reader.readAsDataURL(e.target.files[0]);
            }
        });
    }

    if (previewContainer) {
        const editIcon = previewContainer.querySelector('.avatar-icon.edit');
        const cancelIcon = previewContainer.querySelector('.avatar-icon.cancel');

        if (editIcon) {
            editIcon.addEventListener('click', function() {
                profilePictureInput.click();
            });
        }

        if (cancelIcon) {
            cancelIcon.addEventListener('click', function() {
                previewContainer.classList.add('hidden');
                profilePictureContainer.classList.remove('hidden');
                profilePictureInput.value = '';
            });
        }
    }
}

function initializeAlterEgoHandlers() {
    const createAlterEgoButton = document.getElementById('createAlterEgoButton');
    const alterEgoSelect = document.getElementById('alterEgo');
    const saveAlterEgoButton = document.getElementById('saveAlterEgo');
    const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');
    const alterEgoPictureInput = document.getElementById('alterEgoProfilePicture');
    const previewContainer = document.getElementById('previewAlterContainer');

    if (createAlterEgoButton) {
        createAlterEgoButton.addEventListener('click', function() {
            document.getElementById('alterEgoId').value = '';
            document.getElementById('alterEgoName').value = '';
            document.getElementById('alterEgoDescription').value = '';
            if (alterEgoPictureInput) alterEgoPictureInput.value = '';

            alterEgoPictureContainer.innerHTML = `
                <span class="avatar-initial" id="defaultAlterEgoInitial">P</span>
                <div class="avatar-icons">
                    <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                </div>
            `;

            previewContainer.classList.add('hidden');
            alterEgoPictureContainer.classList.remove('hidden');

            alterEgoModal.show();
        });
    }

    if (alterEgoSelect) {
        alterEgoSelect.addEventListener('change', function() {
            const selectedAlterEgoId = this.value;
            if (selectedAlterEgoId && selectedAlterEgoId !== "0") {
                showAlterEgoDetails(selectedAlterEgoId);
            } else {
                const alterEgoDetails = document.getElementById('alterEgoDetails');
                if (alterEgoDetails) alterEgoDetails.innerHTML = '';
            }
        });
    }

    if (saveAlterEgoButton) {
        saveAlterEgoButton.addEventListener('click', saveAlterEgo);
    }

    if (alterEgoPictureContainer) {
        alterEgoPictureContainer.addEventListener('click', function(e) {
            if (e.target.closest('.avatar-icon.edit')) {
                alterEgoPictureInput.click();
            } else if (e.target.closest('.avatar-icon.delete')) {
                deleteAlterEgoPicture();
            }
        });
    }

    if (alterEgoPictureInput) {
        alterEgoPictureInput.addEventListener('change', function(e) {
            if (e.target.files && e.target.files[0]) {
                let reader = new FileReader();
                reader.onload = function(event) {
                    previewContainer.classList.remove('hidden');
                    document.getElementById('previewAlterEgoImage').src = event.target.result;
                    alterEgoPictureContainer.classList.add('hidden');
                };
                reader.readAsDataURL(e.target.files[0]);
            }
        });
    }

    if (previewContainer) {
        previewContainer.addEventListener('click', function(e) {
            if (e.target.closest('.avatar-icon.edit')) {
                alterEgoPictureInput.click();
            } else if (e.target.closest('.avatar-icon.cancel')) {
                cancelAlterEgoPictureChange();
            }
        });
    }
}

// Add this line at the end of the DOMContentLoaded event listener
document.getElementById('alterEgo').addEventListener('change', function() {
    const selectedAlterEgoId = this.value;
    if (selectedAlterEgoId && selectedAlterEgoId !== "0") {
        showAlterEgoDetails(selectedAlterEgoId);
    } else {
        document.getElementById('alterEgoDetails').innerHTML = '';
    }
});

document.getElementById('deleteAccountBtn').addEventListener('click', (e) => {
    e.preventDefault();
    let step = 1;

    NotificationModal.show('warning', 'Warning', `
        <p class="text-danger"><strong>You are about to delete your account.</strong></p>
        <p>This action will:</p>
        <ul>
            <li>Delete all your personal information</li>
            <li>Remove all your prompts and configurations</li>
            <li>Cancel any active subscriptions</li>
            <li>This action cannot be undone</li>
        </ul>
        <p>Are you sure you want to continue?</p>
    `, {
        allowHtml: true,
        confirmText: 'Continue',
        cancelText: 'Cancel',
        showCancel: true,
        hideOnConfirm: false,
        onConfirm: async (modal) => {
            if (step === 1) {
                step = 2;
                modal.update({
                    message: `
                        <p class="text-danger"><strong>Final confirmation required</strong></p>
                        <p>Please type "DELETE" below to confirm you want to permanently delete your account:</p>
                        <input type="text" class="form-control" id="deleteConfirmInput" placeholder="Type DELETE">
                    `,
                    confirmText: 'Delete Account',
                    allowHtml: true
                });
            } else {
                const confirmInput = document.getElementById('deleteConfirmInput');
                if (confirmInput && confirmInput.value === 'DELETE') {
                    try {
                        const response = await secureFetch('/api/delete-account', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            }
                        });

                        if (!response) return; // Session expired
                        if (response.ok) {
                            FormGuard.navigate('/logout', { bypass: true });
                        } else {
                            const data = await response.json();
                            throw new Error(data.detail || 'Error deleting account');
                        }
                    } catch (error) {
                        modal.update({
                            message: `<p class="text-danger">Error: ${escapeHtml(error.message)}</p>`,
                            showConfirm: false,
                            allowHtml: true
                        });
                    }
                } else {
                    modal.update({
                        message: `
                            <p class="text-danger"><strong>Final confirmation required</strong></p>
                            <p>Please type "DELETE" below to confirm you want to permanently delete your account:</p>
                            <input type="text" class="form-control" id="deleteConfirmInput" placeholder="Type DELETE">
                            <p class="text-danger mt-2">Please type "DELETE" correctly to confirm.</p>
                        `,
                        allowHtml: true
                    });
                }
            }
        }
    });
});

// After Login preference management
function initAfterLoginPreference() {
    const radios = document.querySelectorAll('input[name="afterLogin"]');
    if (!radios.length) return;

    const prefs = (typeof homePreferences !== 'undefined' && homePreferences) ? homePreferences : {};
    const afterLogin = prefs.after_login || '/home';

    const radio = document.querySelector(`input[name="afterLogin"][value="${afterLogin}"]`);
    if (radio) radio.checked = true;
}

function saveAfterLoginPreference() {
    const selectedRadio = document.querySelector('input[name="afterLogin"]:checked');
    if (!selectedRadio) return Promise.resolve();

    return secureFetch('/api/home/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_login: selectedRadio.value })
    })
    .then(response => {
        if (!response) return null;
        if (!response.ok) throw new Error('Failed to save login preference');
        return response.json();
    });
}

function saveWebSearchSettings() {
    const engineRadio = document.querySelector('input[name="wsEngine"]:checked');

    if (!engineRadio) return Promise.resolve();

    return secureFetch('/api/user/web-search-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            web_search_mode: engineRadio.value
        })
    })
    .then(response => {
        if (!response) return null;
        if (!response.ok) throw new Error('Failed to save web search settings');
        return response.json();
    });
}
