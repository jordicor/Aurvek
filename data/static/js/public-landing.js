/* ============================================
   Aurvek Landing Pages - Shared JavaScript
   Engine: NuriAI
   ============================================ */

(function() {
    'use strict';

    const i18n = window.AurvekI18n;

    // ============================================
    // Navigation
    // ============================================

    const nav = document.querySelector('.nav');
    const navToggle = document.querySelector('.nav__toggle');
    const navLinks = document.querySelector('.nav__links');

    // Scroll effect for navigation
    function handleNavScroll() {
        if (window.scrollY > 50) {
            nav?.classList.add('nav--scrolled');
        } else {
            nav?.classList.remove('nav--scrolled');
        }
    }

    window.addEventListener('scroll', handleNavScroll);
    handleNavScroll();

    // Mobile navigation toggle
    navToggle?.addEventListener('click', function() {
        navLinks?.classList.toggle('nav__links--open');

        // Animate hamburger
        const spans = this.querySelectorAll('span');
        this.classList.toggle('active');
    });

    // Close mobile nav when clicking a link
    navLinks?.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', () => {
            navLinks.classList.remove('nav__links--open');
            navToggle?.classList.remove('active');
        });
    });

    // ============================================
    // Smooth Scroll for Anchor Links
    // ============================================

    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function(e) {
            const href = this.getAttribute('href');
            if (href === '#') return;

            const target = document.querySelector(href);
            if (target) {
                e.preventDefault();
                const navHeight = nav?.offsetHeight || 72;
                const targetPosition = target.offsetTop - navHeight - 20;

                window.scrollTo({
                    top: targetPosition,
                    behavior: 'smooth'
                });
            }
        });
    });

    // ============================================
    // Intersection Observer for Animations
    // ============================================

    const observerOptions = {
        threshold: 0.1,
        rootMargin: '0px 0px -50px 0px'
    };

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('animate-fade-in-up');
                observer.unobserve(entry.target);
            }
        });
    }, observerOptions);

    // Observe elements with data-animate attribute
    document.querySelectorAll('[data-animate]').forEach(el => {
        el.style.opacity = '0';
        observer.observe(el);
    });

    // ============================================
    // Revenue Calculator
    // ============================================

    const calculator = document.querySelector('.calculator');

    if (calculator) {
        const usersInput = calculator.querySelector('[data-calc="users"]');
        const messagesInput = calculator.querySelector('[data-calc="messages"]');
        const priceInput = calculator.querySelector('[data-calc="price"]');
        const resultEl = calculator.querySelector('.calculator__amount');

        const usersValueEl = calculator.querySelector('[data-value="users"]');
        const messagesValueEl = calculator.querySelector('[data-value="messages"]');
        const priceValueEl = calculator.querySelector('[data-value="price"]');

        function updateCalculator() {
            const users = parseInt(usersInput?.value || 100);
            const messagesPerUser = parseInt(messagesInput?.value || 50);
            const pricePerMessage = parseFloat(priceInput?.value || 0.01);

            // 70% revenue share
            const monthlyRevenue = users * messagesPerUser * pricePerMessage * 0.70;

            if (resultEl) {
                resultEl.textContent = i18n.formatCurrency(monthlyRevenue, 'USD', 0);
            }

            // Update displayed values
            if (usersValueEl) usersValueEl.textContent = i18n.formatNumber(users);
            if (messagesValueEl) messagesValueEl.textContent = i18n.formatNumber(messagesPerUser);
            if (priceValueEl) priceValueEl.textContent = i18n.formatCurrency(pricePerMessage, 'USD', 3);
        }

        usersInput?.addEventListener('input', updateCalculator);
        messagesInput?.addEventListener('input', updateCalculator);
        priceInput?.addEventListener('input', updateCalculator);

        // Initial calculation
        updateCalculator();
    }

    // ============================================
    // FAQ Accordion
    // ============================================

    document.querySelectorAll('.faq__question').forEach(question => {
        question.addEventListener('click', function() {
            const faqItem = this.parentElement;
            const isOpen = faqItem.classList.contains('faq__item--open');

            // Close all FAQ items
            document.querySelectorAll('.faq__item--open').forEach(item => {
                item.classList.remove('faq__item--open');
            });

            // Open clicked item if it was closed
            if (!isOpen) {
                faqItem.classList.add('faq__item--open');
            }
        });
    });

    // ============================================
    // Tabs (for mode selection)
    // ============================================

    document.querySelectorAll('[data-tabs]').forEach(tabContainer => {
        const tabs = tabContainer.querySelectorAll('[data-tab]');
        const panels = tabContainer.querySelectorAll('[data-panel]');

        tabs.forEach(tab => {
            tab.addEventListener('click', function() {
                const targetPanel = this.dataset.tab;

                // Update active tab
                tabs.forEach(t => t.classList.remove('tab--active'));
                this.classList.add('tab--active');

                // Show target panel
                panels.forEach(panel => {
                    if (panel.dataset.panel === targetPanel) {
                        panel.classList.add('panel--active');
                        panel.hidden = false;
                    } else {
                        panel.classList.remove('panel--active');
                        panel.hidden = true;
                    }
                });
            });
        });
    });

    // ============================================
    // Copy to Clipboard
    // ============================================

    document.querySelectorAll('[data-copy]').forEach(button => {
        button.addEventListener('click', async function() {
            const textToCopy = this.dataset.copy;

            try {
                await navigator.clipboard.writeText(textToCopy);

                const originalText = this.textContent;
                this.textContent = i18n.t('public_shell.copied');
                this.classList.add('btn--success');

                setTimeout(() => {
                    this.textContent = originalText;
                    this.classList.remove('btn--success');
                }, 2000);
            } catch (err) {
                console.error('Failed to copy:', err);
            }
        });
    });

    // ============================================
    // Form Validation (basic)
    // ============================================

    document.querySelectorAll('form[data-validate]').forEach(form => {
        form.addEventListener('submit', function(e) {
            let isValid = true;

            this.querySelectorAll('[required]').forEach(field => {
                if (!field.value.trim()) {
                    isValid = false;
                    field.classList.add('input--error');
                } else {
                    field.classList.remove('input--error');
                }
            });

            // Email validation
            this.querySelectorAll('[type="email"]').forEach(emailField => {
                const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
                if (emailField.value && !emailRegex.test(emailField.value)) {
                    isValid = false;
                    emailField.classList.add('input--error');
                }
            });

            if (!isValid) {
                e.preventDefault();
            }
        });
    });

    // ============================================
    // Lazy Loading Images
    // ============================================

    if ('IntersectionObserver' in window) {
        const imageObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    if (img.dataset.src) {
                        img.src = img.dataset.src;
                        img.removeAttribute('data-src');
                    }
                    imageObserver.unobserve(img);
                }
            });
        });

        document.querySelectorAll('img[data-src]').forEach(img => {
            imageObserver.observe(img);
        });
    }

    // ============================================
    // Analytics Events (placeholder)
    // ============================================

    function trackEvent(category, action, label) {
        // Placeholder for analytics integration
        // Replace with your analytics provider (GA4, Mixpanel, etc.)
        if (window.gtag) {
            window.gtag('event', action, {
                event_category: category,
                event_label: label
            });
        }

        console.log('Event:', category, action, label);
    }

    // Track CTA clicks
    document.querySelectorAll('.btn--primary, .btn--secondary').forEach(btn => {
        btn.addEventListener('click', function() {
            const label = this.textContent.trim();
            const section = this.closest('section')?.id || 'unknown';
            trackEvent('CTA', 'click', `${section}: ${label}`);
        });
    });

    // Track scroll depth
    let maxScrollDepth = 0;
    window.addEventListener('scroll', () => {
        const scrollPercent = Math.round(
            (window.scrollY / (document.body.scrollHeight - window.innerHeight)) * 100
        );

        if (scrollPercent > maxScrollDepth) {
            maxScrollDepth = scrollPercent;

            // Track milestones
            if ([25, 50, 75, 100].includes(maxScrollDepth)) {
                trackEvent('Scroll', 'depth', `${maxScrollDepth}%`);
            }
        }
    });

    // ============================================
    // Countdown Timer (for urgency, if needed)
    // ============================================

    document.querySelectorAll('[data-countdown]').forEach(countdownEl => {
        const endDate = new Date(countdownEl.dataset.countdown);

        function updateCountdown() {
            const now = new Date();
            const diff = endDate - now;

            if (diff <= 0) {
                countdownEl.textContent = i18n.t('public_shell.expired');
                return;
            }

            const days = Math.floor(diff / (1000 * 60 * 60 * 24));
            const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
            const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
            const seconds = Math.floor((diff % (1000 * 60)) / 1000);

            countdownEl.textContent = i18n.t('public_shell.countdown', {days, hours, minutes, seconds});
        }

        updateCountdown();
        setInterval(updateCountdown, 1000);
    });

    // ============================================
    // Video Modal
    // ============================================

    document.querySelectorAll('[data-video]').forEach(trigger => {
        trigger.addEventListener('click', function(e) {
            e.preventDefault();

            const videoUrl = this.dataset.video;

            // Create modal
            const modal = document.createElement('div');
            modal.className = 'video-modal';
            modal.innerHTML = `
                <div class="video-modal__backdrop"></div>
                <div class="video-modal__content">
                    <button class="video-modal__close">&times;</button>
                    <iframe
                        src="${videoUrl}?autoplay=1"
                        frameborder="0"
                        allow="autoplay; fullscreen"
                        allowfullscreen>
                    </iframe>
                </div>
            `;

            document.body.appendChild(modal);
            document.body.style.overflow = 'hidden';

            // Close modal
            modal.querySelector('.video-modal__close').addEventListener('click', closeModal);
            modal.querySelector('.video-modal__backdrop').addEventListener('click', closeModal);

            function closeModal() {
                modal.remove();
                document.body.style.overflow = '';
            }

            document.addEventListener('keydown', function escHandler(e) {
                if (e.key === 'Escape') {
                    closeModal();
                    document.removeEventListener('keydown', escHandler);
                }
            });
        });
    });

    // ============================================
    // Notification/Toast
    // ============================================

    window.showToast = function(message, type = 'info', duration = 3000) {
        const toast = document.createElement('div');
        toast.className = `toast toast--${type}`;
        toast.textContent = message;

        // Styles
        Object.assign(toast.style, {
            position: 'fixed',
            bottom: '20px',
            right: '20px',
            padding: '16px 24px',
            background: type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#6366f1',
            color: 'white',
            borderRadius: '8px',
            boxShadow: '0 10px 25px rgba(0,0,0,0.2)',
            zIndex: '10000',
            animation: 'fadeInUp 0.3s ease'
        });

        document.body.appendChild(toast);

        setTimeout(() => {
            toast.style.animation = 'fadeInUp 0.3s ease reverse';
            setTimeout(() => toast.remove(), 300);
        }, duration);
    };

    // ============================================
    // Initialize
    // ============================================

    console.log('Aurvek Landing initialized - Powered by NuriAI');

})();
