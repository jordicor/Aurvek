// edit_profile.js

let phoneInputJS;
let audioPlayer = null;
let alterEgoModal;
let phoneChallengeId = null;
let challengePhoneNumber = null;
let phoneChallengeApproved = false;
let profileSaveInProgress = false;

function profileText(key, params = {}) {
    return window.AurvekI18n.t(`profile.${key}`, params);
}

function profileUiError(message) {
    const error = new Error(message);
    error.uiSafe = true;
    return error;
}

function profileUiErrorMessage(error, fallbackKey) {
    return error?.uiSafe ? error.message : profileText(fallbackKey);
}

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
    initTimezonePreference();
    initLanguagePreferences();
    initializeAlterEgoState();
    loadVoices().then(function() {
        FormGuard.markFieldsClean('#editProfileForm', ['sample_voice_id']);
    });
    loadAlterEgos(currentAlterEgoId).then(function() {
        FormGuard.markFieldsClean('#editProfileForm', ['alter_ego_id']);
    });
    setupEventListeners();
    initializeProfileHandlers();

    const alterEgoModalElement = document.getElementById('alterEgoModal');
    // Keep the fixed dialog outside theme containers with their own stacking context.
    document.body.appendChild(alterEgoModalElement);
    alterEgoModal = new bootstrap.Modal(alterEgoModalElement);

    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl)
    });

    const deleteProfilePictureButton = document.getElementById('deleteProfilePictureButton');
    if (deleteProfilePictureButton) {
        deleteProfilePictureButton.addEventListener('click', deleteProfilePicture);
    }

    initializeAlterEgoHandlers();

    // The current password verifies a separate action; browser autofill is not a profile edit.
    FormGuard.watch('#editProfileForm', { exclude: ['old_password'] });
});

// Initialize when DOM is ready

function initTimezonePreference() {
    const timezoneSelect = document.getElementById('timezoneName');
    const useDeviceButton = document.getElementById('useDeviceTimezoneButton');
    const status = document.getElementById('deviceTimezoneStatus');
    if (!timezoneSelect || !useDeviceButton) return;

    function selectTimezone(timezone) {
        // Browser and city databases can use different IANA aliases from the server.
        if (!Array.from(timezoneSelect.options).some(option => option.value === timezone)) {
            timezoneSelect.add(new Option(timezone.replaceAll('_', ' ').replaceAll('/', ' / '), timezone));
        }
        timezoneSelect.value = timezone;
        timezoneSelect.dispatchEvent(new Event('input', { bubbles: true }));
        timezoneSelect.dispatchEvent(new Event('change', { bubbles: true }));
    }

    timezoneSelect.addEventListener('change', function() {
        if (status) status.textContent = '';
    });

    useDeviceButton.addEventListener('click', function() {
        const deviceTimezone = Intl.DateTimeFormat(window.AurvekI18n.locale).resolvedOptions().timeZone;
        if (!deviceTimezone) {
            if (status) {
                status.className = 'form-text d-block text-danger';
                status.textContent = profileText('timezone.detect_failed');
            }
            return;
        }

        selectTimezone(deviceTimezone);
        if (status) {
            status.className = 'form-text d-block text-success';
            status.textContent = profileText('timezone.detected', { timezone: deviceTimezone });
        }
    });

    initTimezoneCitySearch(timezoneSelect, selectTimezone, status);
}

function initTimezoneCitySearch(timezoneSelect, selectTimezone, status) {
    const panel = document.getElementById('timezoneCitySearch');
    const input = document.getElementById('timezoneCityInput');
    const searchButton = document.getElementById('timezoneCitySearchButton');
    const searchStatus = document.getElementById('timezoneCityStatus');
    const results = document.getElementById('timezoneCityResults');
    if (!panel || !input || !searchButton || !searchStatus || !results) return;

    let requestVersion = 0;

    function resetSearch() {
        requestVersion += 1;
        searchButton.disabled = false;
        searchStatus.textContent = '';
        results.replaceChildren();
        results.hidden = true;
    }

    function announce(key, isError = false, params = {}) {
        searchStatus.className = `form-text d-block ${isError ? 'text-danger' : 'text-muted'}`;
        searchStatus.textContent = profileText(`timezone.${key}`, params);
    }

    async function searchCities() {
        if (searchButton.disabled) return;
        resetSearch();
        const query = input.value.trim();
        if (query.length < 2) {
            announce('city_search_short', true);
            input.focus();
            return;
        }

        const version = requestVersion;
        searchButton.disabled = true;
        announce('city_search_loading');
        try {
            const response = await secureFetch(`/api/profile/timezone-cities?q=${encodeURIComponent(query)}`);
            if (!response || !response.ok) throw new Error('City search failed');
            const data = await response.json();
            // Editing the query or selecting a zone invalidates an earlier response.
            if (version !== requestVersion) return;
            if (!Array.isArray(data.results)) throw new Error('Invalid city search results');
            data.results.forEach(function(city) {
                const place = [city.city, city.region, city.country].filter(Boolean).join(' · ');
                const row = document.createElement('li');
                row.className = 'list-group-item d-flex align-items-start justify-content-between gap-2';
                const description = document.createElement('div');
                description.className = 'text-break';
                const label = document.createElement('div');
                label.className = 'fw-semibold';
                label.textContent = place;
                const zone = document.createElement('small');
                zone.className = 'd-block text-muted';
                zone.textContent = profileText('timezone.city_search_zone', { timezone: city.timezone });
                description.append(label, zone);
                const useButton = document.createElement('button');
                useButton.type = 'button';
                useButton.className = 'btn btn-sm btn-outline-primary flex-shrink-0';
                useButton.textContent = profileText('timezone.city_search_use');
                useButton.setAttribute('aria-label', `${useButton.textContent}: ${place}`);
                useButton.addEventListener('click', function() {
                    selectTimezone(city.timezone);
                    if (status) {
                        status.className = 'form-text d-block text-success';
                        status.textContent = profileText('timezone.city_search_selected', {
                            city: place, timezone: city.timezone,
                        });
                    }
                    panel.open = false;
                    timezoneSelect.focus();
                });
                row.append(description, useButton);
                results.append(row);
            });
            results.hidden = data.results.length === 0;
            if (data.results.length) {
                announce('city_search_results', false, { count: data.results.length });
            } else {
                announce('city_search_empty');
            }
        } catch (error) {
            if (version === requestVersion) announce('city_search_error', true);
        } finally {
            if (version === requestVersion) searchButton.disabled = false;
        }
    }

    input.addEventListener('input', resetSearch);
    timezoneSelect.addEventListener('change', resetSearch);
    searchButton.addEventListener('click', searchCities);
    input.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' && !event.isComposing) {
            event.preventDefault();
            searchCities();
        }
    });
}

function initLanguagePreferences() {
    const root = document.querySelector('[data-language-preferences]');
    if (!root) return;

    const primaryInput = root.querySelector('#primaryLanguageInput');
    const secondaryInput = root.querySelector('#additionalLanguageInput');
    const hiddenInput = root.querySelector('#preferredLanguagesJson');
    const chips = root.querySelector('#additionalLanguageChips');
    const showAdditionalButton = root.querySelector('#showAdditionalLanguageButton');
    const controls = root.querySelector('#additionalLanguageControls');
    const addButton = root.querySelector('#addAdditionalLanguageButton');
    const status = root.querySelector('#languagePreferenceStatus');
    const maxLanguages = Number.parseInt(root.dataset.maxLanguages || '6', 10);
    const optionElements = Array.from(root.querySelectorAll('#userLanguageOptions option'));
    const codeToLabel = new Map();
    const valueToCode = new Map();

    optionElements.forEach(function(option) {
        const code = String(option.dataset.code || '').toLowerCase();
        const label = String(option.value || '').trim();
        if (!code || !label) return;
        codeToLabel.set(code, label);
        valueToCode.set(code, code);
        valueToCode.set(label.toLowerCase(), code);
        valueToCode.set(`${label.toLowerCase()} (${code})`, code);
    });

    let languages = [];
    try {
        const parsed = JSON.parse(hiddenInput.value || '[]');
        if (Array.isArray(parsed)) {
            languages = parsed
                .map(function(code) { return String(code || '').toLowerCase(); })
                .filter(function(code, index, values) {
                    return codeToLabel.has(code) && values.indexOf(code) === index;
                })
                .slice(0, maxLanguages);
        }
    } catch (error) {
        languages = [];
    }

    function languageCode(value) {
        return valueToCode.get(String(value || '').trim().toLowerCase()) || null;
    }

    function announce(message, isError) {
        status.textContent = message || '';
        status.className = isError
            ? 'form-text d-block text-danger'
            : 'form-text d-block text-success';
    }

    function syncHiddenInput() {
        const serialized = JSON.stringify(languages);
        if (hiddenInput.value === serialized) return;
        hiddenInput.value = serialized;
        hiddenInput.dispatchEvent(new Event('input', { bubbles: true }));
        hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function renderLanguages() {
        const primary = languages[0] || '';
        primaryInput.value = primary ? codeToLabel.get(primary) : '';
        chips.innerHTML = '';
        languages.slice(1).forEach(function(code) {
            const chip = document.createElement('span');
            chip.className = 'badge rounded-pill text-bg-light border d-inline-flex align-items-center gap-1';

            const label = document.createElement('span');
            label.textContent = codeToLabel.get(code) || code;
            chip.appendChild(label);

            const removeButton = document.createElement('button');
            removeButton.type = 'button';
            removeButton.className = 'btn btn-sm p-0 border-0 bg-transparent text-secondary';
            removeButton.dataset.removeLanguage = code;
            removeButton.setAttribute('aria-label', profileText('language.remove', { language: label.textContent }));
            removeButton.textContent = '\u00d7';
            chip.appendChild(removeButton);
            chips.appendChild(chip);
        });
        showAdditionalButton.disabled = !primary || languages.length >= maxLanguages;
        if (!primary || languages.length >= maxLanguages) {
            controls.hidden = true;
        }
        syncHiddenInput();
    }

    function applyPrimaryLanguage() {
        const rawValue = primaryInput.value.trim();
        if (!rawValue) {
            languages = [];
            announce(profileText('language.automatic'), false);
            renderLanguages();
            return;
        }

        const code = languageCode(rawValue);
        if (!code) {
            primaryInput.value = languages[0] ? codeToLabel.get(languages[0]) : '';
            announce(profileText('language.choose_from_list'), true);
            return;
        }

        const previousPrimary = languages[0];
        if (code !== previousPrimary && languages.includes(code)) {
            languages = [
                code,
                previousPrimary,
                ...languages.slice(1).filter(function(item) { return item !== code; }),
            ].filter(Boolean);
        } else if (code !== previousPrimary) {
            languages = [code, ...languages.slice(1)];
        }
        announce(profileText('language.primary_changed', { language: codeToLabel.get(code) }), false);
        renderLanguages();
    }

    showAdditionalButton.addEventListener('click', function() {
        if (!languages.length) {
            announce(profileText('language.choose_primary_first'), true);
            primaryInput.focus();
            return;
        }
        controls.hidden = false;
        secondaryInput.focus();
    });

    addButton.addEventListener('click', function() {
        const code = languageCode(secondaryInput.value);
        if (!code) {
            announce(profileText('language.choose_from_list'), true);
            return;
        }
        if (!languages.length) {
            announce(profileText('language.choose_primary_first'), true);
            primaryInput.focus();
            return;
        }
        if (languages.includes(code)) {
            announce(profileText('language.already_selected', { language: codeToLabel.get(code) }), true);
            return;
        }
        if (languages.length >= maxLanguages) {
            announce(profileText('language.maximum', { count: maxLanguages }), true);
            return;
        }
        languages.push(code);
        secondaryInput.value = '';
        announce(profileText('language.added', { language: codeToLabel.get(code) }), false);
        renderLanguages();
        if (languages.length < maxLanguages) secondaryInput.focus();
    });

    secondaryInput.addEventListener('keydown', function(event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            addButton.click();
        }
    });
    primaryInput.addEventListener('change', applyPrimaryLanguage);
    primaryInput.addEventListener('input', function() {
        if (!primaryInput.value.trim()) applyPrimaryLanguage();
    });
    primaryInput.addEventListener('keydown', function(event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            applyPrimaryLanguage();
        }
    });
    chips.addEventListener('click', function(event) {
        const removeButton = event.target.closest('[data-remove-language]');
        if (!removeButton) return;
        const code = removeButton.dataset.removeLanguage;
        languages = languages.filter(function(item, index) {
            return index === 0 || item !== code;
        });
        announce(profileText('language.removed', { language: codeToLabel.get(code) || code }), false);
        renderLanguages();
    });

    renderLanguages();
}

function initPhoneInput() {
    const phoneInputField = document.getElementById('phone');
    let editedDuringLoad = false;
    function trackPhoneEdit() { editedDuringLoad = true; }
    phoneInputField.addEventListener('input', trackPhoneEdit);
    const regionNames = new Intl.DisplayNames([window.AurvekI18n.locale], { type: 'region' });
    phoneInputJS = window.intlTelInput(phoneInputField, {
        initialCountry: "auto",
        separateDialCode: true,
        localizedCountries: Object.fromEntries(window.intlTelInputGlobals.getCountryData().map(
            country => [country.iso2, regionNames.of(country.iso2.toUpperCase())]
        )),
        utilsScript: "https://cdnjs.cloudflare.com/ajax/libs/intl-tel-input/17.0.13/js/utils.js",
        geoIpLookup: function(success, failure) {
            secureFetch('/api/get-ip-info')
                .then(function(response) {
                    if (!response) return null;
                    if (response.ok) return response.json();
                    throw new Error('Failed to fetch IP info');
                })
                .then(function(ipinfo) {
                    success(ipinfo?.country || 'us');
                })
                .catch(function() {
                    success("us");
                });
        },
    });
    function finishPhoneInitialization() {
        phoneInputField.removeEventListener('input', trackPhoneEdit);
        // The phone widget formats saved numbers after its utilities load.
        // Only that field becomes clean; an early user edit must stay protected.
        if (!editedDuringLoad) {
            FormGuard.markFieldsClean('#editProfileForm', ['phone_number']);
        }
    }
    phoneInputJS.promise.then(finishPhoneInitialization, finishPhoneInitialization);
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
    return secureFetch('/api/voices')
        .then(response => {
            if (!response) return null;
            return response.json();
        })
        .then(voices => {
            if (!voices) return;
            voiceSelect.replaceChildren();
            const defaultOption = document.createElement('option');
            defaultOption.value = '';
            defaultOption.textContent = profileText('voice.default');
            voiceSelect.appendChild(defaultOption);
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
    categorySelect.style.maxWidth = '100%';

    const categoryKeys = [
        'children', 'finance', 'relaxation', 'casual', 'drama', 'storytelling',
        'advertising', 'science', 'education', 'corporate', 'mystery', 'sports'
    ];

    categoryKeys.forEach((categoryKey, index) => {
        const option = document.createElement('option');
        option.value = index;
        option.textContent = profileText(`voice.category.${categoryKey}`);
        categorySelect.appendChild(option);
    });

    const playButton = document.createElement('button');
    playButton.textContent = profileText('voice.play_sample');
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
        playButton.textContent = profileText('voice.play_sample');
        return;
    }

    const playButton = document.getElementById('playVoiceButton');
    playButton.textContent = profileText('voice.loading');
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
                playButton.textContent = profileText('voice.play_sample');
                playButton.disabled = false;
                audioPlayer = null;
            };

            audioPlayer.play();
            playButton.textContent = profileText('voice.stop_sample');
            playButton.disabled = false;

            playButton.onclick = function() {
                if (audioPlayer) {
                    audioPlayer.pause();
                    audioPlayer.currentTime = 0;
                    audioPlayer = null;
                    playButton.textContent = profileText('voice.play_sample');
                }
            };
        })
        .catch(error => {
            console.error('Error playing voice sample:', error);
            playButton.textContent = profileText('voice.play_sample');
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
    verifyCodeButton.textContent = profileText('phone.verify_code');
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
            NotificationModal.error(profileText('modal.error'), profileText('phone.request_new_code'));
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
                NotificationModal.success(profileText('modal.success'), profileText('phone.verified'));
                verifyCodeButton.style.display = 'none';
                verificationCodeInput.disabled = true;
                phoneInput.dataset.verified = 'true';
                phoneChallengeApproved = true;
                phoneVerificationIdInput.value = phoneChallengeId;
            } else {
                NotificationModal.error(profileText('modal.error'), result.detail || profileText('phone.verification_failed'));
                verificationCodeInput.value = '';
            }
        } catch (error) {
            console.error('Error:', error);
            NotificationModal.error(profileText('modal.error'), profileText('phone.verification_error'));
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
                NotificationModal.error(profileText('modal.error'), profileText('phone.in_use'));
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
                    NotificationModal.success(profileText('modal.success'), profileText('phone.code_sent'));
                } else {
                    NotificationModal.error(profileText('modal.error'), profileText('phone.unexpected_status', { status: result.status }));
                }
            } else {
                NotificationModal.error(profileText('modal.error'), result.detail || profileText('phone.send_failed'));
            }
        } catch (error) {
            console.error('Error:', error);
            NotificationModal.error(profileText('modal.error'), profileText('error.unexpected'));
        }
    });

    editProfileForm.addEventListener('submit', handleFormSubmit);
}

async function handleFormSubmit(event) {
    event.preventDefault();
    if (profileSaveInProgress) return;
    profileSaveInProgress = true;
    const form = event.target;
    // Edits remain drafts even if the user returns to the pre-save baseline.
    let editedDuringSave = false;
    const trackEdit = (editEvent) => {
        if (editEvent.target.name !== 'old_password' && editEvent.target.id !== 'old-password') {
            editedDuringSave = true;
        }
    };
    form.addEventListener('input', trackEdit);
    form.addEventListener('change', trackEdit);
    const acceptSavedProfile = () => {
        if (editedDuringSave) FormGuard.markDirty(form);
        else FormGuard.markClean(form);
    };
    try {
        const formData = new FormData(event.target);
        const afterLogin = document.querySelector('input[name="afterLogin"]:checked')?.value;
        const webSearchMode = document.querySelector('input[name="wsEngine"]:checked')?.value;
        const useAlterEgo = document.getElementById('useAlterEgo').checked;
        const alterEgoId = document.getElementById('alterEgo').value;
        formData.set('alter_ego_id', useAlterEgo && alterEgoId !== '0' ? alterEgoId : '0');

        const fullPhoneNumber = getFullPhoneNumber();
        formData.set('phone_number', fullPhoneNumber);

        const phoneInput = document.getElementById('phone');
        const phoneNeedsVerification = Boolean(fullPhoneNumber)
            && fullPhoneNumber !== originalPhoneNumber;

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
                    NotificationModal.error(profileText('modal.error'), profileText('phone.in_use'));
                    return;
                }

                if (
                    phoneInput.dataset.verified !== 'true'
                    || !phoneChallengeApproved
                    || challengePhoneNumber !== fullPhoneNumber
                    || !phoneChallengeId
                ) {
                    NotificationModal.error(profileText('modal.error'), profileText('phone.verify_before_save'));
                    return;
                }
                formData.set('phone_verification_id', phoneChallengeId);
            } catch (error) {
                console.error('Error:', error);
                NotificationModal.error(profileText('modal.error'), profileText('phone.check_error'));
                return;
            }
        }

        await secureFetch('/api/edit-profile', {
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
        .then(async data => {
            if (!data) return;
            if (data.success) {
                currentUserVoiceId = formData.get('sample_voice_id');
                originalPhoneNumber = fullPhoneNumber;
                if (phoneChallengeApproved && challengePhoneNumber === fullPhoneNumber) {
                    document.getElementById('phone').dataset.phoneVerified = 'true';
                }
                currentAlterEgoId = formData.get('alter_ego_id');
                if (data.reauthenticate) {
                    acceptSavedProfile();
                    NotificationModal.success(profileText('modal.success'), profileText('phone.updated_reauthenticate'));
                    setTimeout(() => {
                        FormGuard.navigate('/login');
                    }, 1200);
                    return;
                }
                // Keep this save active until both requests finish, including partial failures.
                const results = await Promise.allSettled([
                    saveAfterLoginPreference(afterLogin), saveWebSearchSettings(webSearchMode)
                ]);
                const failed = results.find(result => result.status === 'rejected');
                if (failed) {
                    NotificationModal.error(
                        profileText('modal.error'),
                        profileUiErrorMessage(failed.reason, 'error.preferences_save')
                    );
                    return;
                }
                acceptSavedProfile();
                NotificationModal.success(profileText('modal.success'), data.message || profileText('saved'));
                if (data.ui_language && data.ui_language !== window.AurvekI18n.language) {
                    FormGuard.reloadIfClean();
                }
            } else {
                NotificationModal.error(profileText('modal.error'), data.message || profileText('error.profile_update'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            NotificationModal.error(profileText('modal.error'), profileText('error.profile_update'));
        });
    } finally {
        if (editedDuringSave) FormGuard.markDirty(form);
        FormGuard.resumeAfterSubmit(form);
        form.removeEventListener('input', trackEdit);
        form.removeEventListener('change', trackEdit);
        profileSaveInProgress = false;
    }
}

function getFullPhoneNumber() {
    return phoneInputJS.getNumber(intlTelInputUtils.numberFormat.E164);
}

// Functions related to alter-ego management
function loadAlterEgos(currentAlterEgoId = document.getElementById('alterEgo').value) {
    return secureFetch('/api/get-alter-egos', {
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
            alterEgoSelect.replaceChildren();
            const emptyOption = document.createElement('option');
            emptyOption.value = '0';
            emptyOption.textContent = profileText('alter_ego.select_placeholder');
            alterEgoSelect.appendChild(emptyOption);
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
                ? `<img src="${escapeHtml(data.alterEgo.profilePicture)}" alt="${escapeHtml(profileText('alter_ego.picture'))}" style="width: 100px; height: 100px; object-fit: cover; border-radius: 50%;">`
                : `<div style="width: 100px; height: 100px; background-color: #f0f0f0; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 2em;">${escapeHtml(data.alterEgo.name.charAt(0).toUpperCase())}</div>`;

            alterEgoDetails.innerHTML = `
                <div class="alter-ego-container">
                    <div class="alter-ego-header">
                        ${profilePicture}
                        <div>
                            <h3>${escapeHtml(data.alterEgo.name)}</h3>
                            <p>${escapeHtml(data.alterEgo.description || profileText('alter_ego.no_description'))}</p>
                        </div>
                    </div>
                    <div class="btn-group">
                        <button type="button" class="btn btn-primary" id="editAlterEgoButton"></button>
                        <button type="button" class="btn btn-danger" id="deleteAlterEgoButton"></button>
                    </div>
                </div>
            `;

            document.getElementById('editAlterEgoButton').textContent = profileText('action.edit');
            document.getElementById('deleteAlterEgoButton').textContent = profileText('action.delete');
            document.getElementById('editAlterEgoButton').addEventListener('click', function() {
                editAlterEgo(alterEgoId);
            });
            document.getElementById('deleteAlterEgoButton').addEventListener('click', function() {
                deleteAlterEgo(alterEgoId);
            });
        } else {
            console.error('Error loading alter-ego details:', data);
            alterEgoDetails.replaceChildren();
            const message = document.createElement('p');
            message.textContent = profileText('alter_ego.load_failed');
            alterEgoDetails.appendChild(message);
        }
    })
    .catch(error => {
        console.error('Error fetching alter-ego details:', error);
        alterEgoDetails.replaceChildren();
        const message = document.createElement('p');
        message.textContent = profileText('alter_ego.load_error');
        alterEgoDetails.appendChild(message);
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
            document.getElementById('alterEgoProfilePicture').value = '';
            document.getElementById('previewAlterEgoImage').removeAttribute('src');

            const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');
            if (data.alterEgo.profilePicture) {
                alterEgoPictureContainer.innerHTML = `
                    <img src="${escapeHtml(data.alterEgo.profilePicture)}" alt="${escapeHtml(profileText('alter_ego.picture'))}" id="currentAlterEgoPicture">
                    <div class="avatar-icons">
                        <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                        <span class="avatar-icon delete"><i class="fas fa-trash"></i></span>
                    </div>
                `;
            } else {
                const initial = data.alterEgo.name.charAt(0).toUpperCase();
                alterEgoPictureContainer.innerHTML = `
                    <span class="avatar-initial" id="defaultAlterEgoInitial">${escapeHtml(initial)}</span>
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
            NotificationModal.error(profileText('modal.error'), profileText('alter_ego.edit_load_failed'));
        }
    })
    .catch(error => {
        console.error('Error fetching alter-ego details:', error);
        NotificationModal.error(profileText('modal.error'), profileText('alter_ego.load_error'));
    });
}

function deleteAlterEgo(alterEgoId) {
    NotificationModal.confirm(
        profileText('alter_ego.delete_title'),
        profileText('alter_ego.delete_confirm'),
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
                    NotificationModal.success(profileText('modal.success'), profileText('alter_ego.deleted'));
                    loadAlterEgos('0');
                    const alterEgoDetails = document.getElementById('alterEgoDetails');
                    if (alterEgoDetails) {
                        alterEgoDetails.innerHTML = '';
                    }
                    const alterEgoSelect = document.getElementById('alterEgo');
                    if (alterEgoSelect) {
                        alterEgoSelect.value = '0';
                    }
                } else {
                    throw profileUiError(data.message || profileText('alter_ego.delete_failed'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                NotificationModal.error(
                    profileText('modal.error'),
                    profileUiErrorMessage(error, 'alter_ego.delete_failed')
                );
            });
        },
        null,
        { type: 'error', confirmText: profileText('action.delete') }
    );
}

function deleteAlterEgoPicture() {
    NotificationModal.confirm(
        profileText('alter_ego.delete_picture_title'),
        profileText('alter_ego.delete_picture_confirm'),
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
                        NotificationModal.success(profileText('modal.success'), profileText('alter_ego.picture_deleted'));
                    } else {
                        NotificationModal.error(profileText('modal.error'), profileText('alter_ego.picture_delete_failed'));
                    }
                })
                .catch(error => {
                    console.error('Error deleting alter-ego picture:', error);
                    NotificationModal.error(profileText('modal.error'), profileText('alter_ego.picture_delete_failed'));
                });
            } else {
                updateAlterEgoPictureUI();
            }
        },
        null,
        { type: 'error', confirmText: profileText('action.delete') }
    );
}

function updateAlterEgoPictureUI(alterEgoId) {
    const alterEgoPictureContainer = document.getElementById('alterEgoPictureContainer');
    const alterEgoName = document.getElementById('alterEgoName').value;
    alterEgoPictureContainer.innerHTML = `
        <span class="avatar-initial" id="defaultAlterEgoInitial">${escapeHtml(alterEgoName.charAt(0).toUpperCase())}</span>
        <div class="avatar-icons">
            <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
        </div>
    `;

    if (alterEgoId) {
        const alterEgoDetails = document.getElementById('alterEgoDetails');
        if (alterEgoDetails) {
            const profilePicture = `<div style="width: 100px; height: 100px; background-color: #f0f0f0; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 2em;">${escapeHtml(alterEgoName.charAt(0).toUpperCase())}</div>`;
            const headerElement = alterEgoDetails.querySelector('.alter-ego-header');
            if (headerElement) {
                headerElement.innerHTML = `
                    ${profilePicture}
                    <div>
                        <h3>${escapeHtml(alterEgoName)}</h3>
                        <p>${escapeHtml(document.getElementById('alterEgoDescription').value || profileText('alter_ego.no_description'))}</p>
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
    const form = document.getElementById('alterEgoForm');
    if (!form.reportValidity()) return;

    const formData = new FormData(form);
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
            return response.json().then(errorData => {
                throw profileUiError(errorData.message || profileText('alter_ego.save_failed'));
            });
        }
        return response.json();
    })
    .then(data => {
        if (!data) return;
        if (data.success) {
            NotificationModal.success(profileText('modal.success'), profileText('alter_ego.saved'));
            alterEgoModal.hide();
            loadAlterEgos();
        } else {
            throw profileUiError(data.message || profileText('alter_ego.save_failed'));
        }
    })
    .catch(error => {
        console.error('Error:', error);
        NotificationModal.error(
            profileText('modal.error'),
            profileUiErrorMessage(error, 'alter_ego.save_failed')
        );
    });
}

function deleteProfilePicture() {
    NotificationModal.confirm(
        profileText('user.delete_picture_title'),
        profileText('user.delete_picture_confirm'),
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
                        <span class="avatar-initial" id="defaultProfileInitial">${escapeHtml(document.getElementById('username').value.charAt(0).toUpperCase())}</span>
                        <div class="avatar-icons">
                            <span class="avatar-icon edit"><i class="fas fa-pencil-alt"></i></span>
                        </div>
                    `;
                    NotificationModal.success(profileText('modal.success'), profileText('user.picture_deleted'));
                } else {
                    NotificationModal.error(profileText('modal.error'), profileText('user.picture_delete_failed'));
                }
            });
        },
        null,
        { type: 'error', confirmText: profileText('action.delete') }
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
            document.getElementById('previewAlterEgoImage').removeAttribute('src');

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

document.getElementById('deleteAccountBtn').addEventListener('click', (e) => {
    e.preventDefault();
    let step = 1;
    const deleteToken = 'DELETE';

    NotificationModal.show('warning', profileText('modal.warning'), `
        <p class="text-danger"><strong>${escapeHtml(profileText('delete_account.warning'))}</strong></p>
        <p>${escapeHtml(profileText('delete_account.effects_intro'))}</p>
        <ul>
            <li>${escapeHtml(profileText('delete_account.effect_personal'))}</li>
            <li>${escapeHtml(profileText('delete_account.effect_prompts'))}</li>
            <li>${escapeHtml(profileText('delete_account.effect_subscriptions'))}</li>
            <li>${escapeHtml(profileText('delete_account.effect_irreversible'))}</li>
        </ul>
        <p>${escapeHtml(profileText('delete_account.continue_question'))}</p>
    `, {
        allowHtml: true,
        confirmText: profileText('action.continue'),
        cancelText: profileText('action.cancel'),
        showCancel: true,
        hideOnConfirm: false,
        onConfirm: async (modal) => {
            if (step === 1) {
                step = 2;
                modal.update({
                    message: `
                        <p class="text-danger"><strong>${escapeHtml(profileText('delete_account.final_confirmation'))}</strong></p>
                        <p>${escapeHtml(profileText('delete_account.type_confirmation', { token: deleteToken }))}</p>
                        <input type="text" class="form-control" id="deleteConfirmInput" placeholder="${escapeHtml(profileText('delete_account.type_token', { token: deleteToken }))}">
                    `,
                    confirmText: profileText('delete_account.delete_action'),
                    allowHtml: true
                });
            } else {
                const confirmInput = document.getElementById('deleteConfirmInput');
                if (confirmInput && confirmInput.value === deleteToken) {
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
                            throw profileUiError(data.detail || profileText('delete_account.failed'));
                        }
                    } catch (error) {
                        modal.update({
                            message: `<p class="text-danger">${escapeHtml(profileText('modal.error'))}: ${escapeHtml(profileUiErrorMessage(error, 'delete_account.failed'))}</p>`,
                            showConfirm: false,
                            allowHtml: true
                        });
                    }
                } else {
                    modal.update({
                        message: `
                            <p class="text-danger"><strong>${escapeHtml(profileText('delete_account.final_confirmation'))}</strong></p>
                            <p>${escapeHtml(profileText('delete_account.type_confirmation', { token: deleteToken }))}</p>
                            <input type="text" class="form-control" id="deleteConfirmInput" placeholder="${escapeHtml(profileText('delete_account.type_token', { token: deleteToken }))}">
                            <p class="text-danger mt-2">${escapeHtml(profileText('delete_account.type_correctly', { token: deleteToken }))}</p>
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

function saveAfterLoginPreference(afterLogin) {
    if (!afterLogin) return Promise.resolve();

    return secureFetch('/api/home/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ after_login: afterLogin })
    })
    .then(response => {
        if (!response || !response.ok) throw profileUiError(profileText('error.after_login_save'));
        return response.json();
    });
}

function saveWebSearchSettings(webSearchMode) {
    if (!webSearchMode) return Promise.resolve();

    return secureFetch('/api/user/web-search-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            web_search_mode: webSearchMode
        })
    })
    .then(response => {
        if (!response || !response.ok) throw profileUiError(profileText('error.web_search_save'));
        return response.json();
    });
}
