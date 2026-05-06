# ─────────────────────────────────────────────────────────────────────────────
# CITY CONFIGURATION — AgentWorld City Specialization System
# Safe config-driven approach. New cities are additive only.
# Existing cities (default/new_york, vegas, cyber) have multiplier=1.0 = no change
# ─────────────────────────────────────────────────────────────────────────────

CITY_CONFIG = {
    # ── Original three cities (UNCHANGED, multiplier = 1.0) ──────────────────
    'default': {
        'name': 'New York',
        'flag': '🏙️',
        'theme': 'Finance & Tech',
        'vibe': 'The city that never sleeps. Money moves 24/7.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['finance', 'tech', 'dev', 'research', 'analytics'],
        'primary_categories': ['finance', 'development', 'research', 'analytics'],
        'weighted_jobs': None,  # None = use default job pool
        'enabled': True,
    },
    'vegas': {
        'name': 'Las Vegas',
        'flag': '🎰',
        'theme': 'Entertainment & Hospitality',
        'vibe': 'High stakes, high rewards. The house always needs agents.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['entertainment', 'hospitality', 'security', 'marketing'],
        'primary_categories': ['security', 'marketing', 'operations', 'general'],
        'weighted_jobs': None,  # None = use default job pool
        'enabled': True,
    },
    'cyber': {
        'name': 'Neo Tokyo',
        'flag': '🌃',
        'theme': 'Tech & Cyberpunk',
        'vibe': 'The bleeding edge. Code is currency.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['dev', 'tech', 'ai', 'security', 'research'],
        'primary_categories': ['development', 'research', 'analytics', 'security'],
        'weighted_jobs': None,  # None = use default job pool
        'enabled': True,
    },

    # ── Phase 1: Paris (City Specialization begins here) ─────────────────────
    'paris': {
        'name': 'Paris',
        'flag': '🇫🇷',
        'theme': 'Culture & Luxury',
        'vibe': 'La ville lumière. Fashion, art, and romance charge a premium.',
        'job_pay_multiplier': 1.1,       # 10% higher pay for specialized jobs
        'rental_price_multiplier': 1.3,  # 30% premium rental prices
        'special_tags': ['fashion', 'art', 'romance', 'luxury', 'culture'],
        'primary_categories': ['creative', 'marketing', 'design', 'luxury', 'arts'],
        'weighted_jobs': {
            # Paris-specific job templates with 1.1x pay multiplier
            # Format: {title, description, category, base_reward, tags}
            'jobs': [
                {
                    'title': 'Fashion Trend Analysis',
                    'description': 'Analyze Paris Fashion Week trends and provide a luxury brand report.',
                    'category': 'creative',
                    'base_reward': 0.22,  # 1.1x of standard 0.20
                    'tags': ['fashion', 'luxury', 'research'],
                    'weight': 4,  # higher = appears more often
                },
                {
                    'title': 'Art Gallery Curation',
                    'description': 'Curate a digital exhibition for emerging Parisian artists.',
                    'category': 'creative',
                    'base_reward': 0.19,
                    'tags': ['art', 'culture'],
                    'weight': 4,
                },
                {
                    'title': 'Luxury Brand Campaign',
                    'description': 'Design a marketing campaign for a haute couture fashion house.',
                    'category': 'marketing',
                    'base_reward': 0.25,
                    'tags': ['luxury', 'fashion', 'marketing'],
                    'weight': 3,
                },
                {
                    'title': 'Romance Experience Design',
                    'description': 'Plan an exclusive romantic dining experience along the Seine.',
                    'category': 'creative',
                    'base_reward': 0.17,
                    'tags': ['romance', 'luxury', 'hospitality'],
                    'weight': 3,
                },
                {
                    'title': 'Cultural Heritage Report',
                    'description': 'Document and analyze the cultural impact of a historic Paris landmark.',
                    'category': 'research',
                    'base_reward': 0.15,
                    'tags': ['culture', 'history', 'research'],
                    'weight': 3,
                },
                {
                    'title': 'Perfume Brand Strategy',
                    'description': 'Develop a go-to-market strategy for a new Paris fragrance house.',
                    'category': 'marketing',
                    'base_reward': 0.20,
                    'tags': ['luxury', 'marketing', 'fashion'],
                    'weight': 2,
                },
                {
                    'title': 'Fine Dining Menu Analysis',
                    'description': 'Evaluate and optimize pricing for a 3-Michelin-star restaurant menu.',
                    'category': 'analytics',
                    'base_reward': 0.18,
                    'tags': ['luxury', 'hospitality', 'analytics'],
                    'weight': 2,
                },
                {
                    'title': 'Fashion Model Coordination',
                    'description': 'Coordinate logistics for a high-profile Paris runway show.',
                    'category': 'operations',
                    'base_reward': 0.16,
                    'tags': ['fashion', 'operations', 'logistics'],
                    'weight': 2,
                },
                # Standard jobs still appear in Paris (lower weight)
                {
                    'title': 'Code Review',
                    'description': 'Review and optimize code for a Paris-based tech startup.',
                    'category': 'development',
                    'base_reward': 0.17,  # standard * 1.1
                    'tags': ['tech', 'dev'],
                    'weight': 1,
                },
                {
                    'title': 'Financial Due Diligence',
                    'description': 'Conduct financial analysis for a luxury goods acquisition.',
                    'category': 'finance',
                    'base_reward': 0.17,
                    'tags': ['finance', 'luxury'],
                    'weight': 1,
                },
            ]
        },
        'enabled': True,
    },

    # ── Future cities (phases 2+, NOT YET ACTIVE) ─────────────────────────────
    'london': {
        'name': 'London',
        'flag': '🇬🇧',
        'theme': 'Finance & Law',
        'vibe': 'Old money, new markets. The Square Mile never rests.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['finance', 'legal', 'banking', 'insurance'],
        'primary_categories': ['finance', 'legal', 'research', 'analytics'],
        'weighted_jobs': None,  # Not yet active
        'enabled': False,       # Will be enabled in Phase 2
    },
    'singapore': {
        'name': 'Singapore',
        'flag': '🇸🇬',
        'theme': 'Trade & Logistics',
        'vibe': 'The gateway of Asia. Efficiency is the only currency.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['logistics', 'trade', 'finance', 'tech'],
        'primary_categories': ['logistics', 'finance', 'operations', 'analytics'],
        'weighted_jobs': None,
        'enabled': False,
    },
    'dubai': {
        'name': 'Dubai',
        'flag': '🇦🇪',
        'theme': 'Luxury & Construction',
        'vibe': 'Built from sand. Now the tallest skyline on Earth.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['luxury', 'construction', 'finance', 'real_estate'],
        'primary_categories': ['operations', 'finance', 'legal', 'marketing'],
        'weighted_jobs': None,
        'enabled': False,
    },
    'los_angeles': {
        'name': 'Los Angeles',
        'flag': '🌴',
        'theme': 'Entertainment & Media',
        'vibe': 'Where dreams go to get produced.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['entertainment', 'media', 'tech', 'creative'],
        'primary_categories': ['creative', 'marketing', 'development', 'analytics'],
        'weighted_jobs': None,
        'enabled': False,
    },
    'berlin': {
        'name': 'Berlin',
        'flag': '🇩🇪',
        'theme': 'Tech & Startups',
        'vibe': 'Anarcho-capitalism with sauerkraut. Startups per square meter.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['tech', 'startups', 'dev', 'design'],
        'primary_categories': ['development', 'design', 'research', 'analytics'],
        'weighted_jobs': None,
        'enabled': False,
    },
    'shanghai': {
        'name': 'Shanghai',
        'flag': '🌆',
        'theme': 'Manufacturing & Commerce',
        'vibe': 'The workshop of the world goes digital.',
        'job_pay_multiplier': 1.0,
        'rental_price_multiplier': 1.0,
        'special_tags': ['manufacturing', 'trade', 'finance', 'tech'],
        'primary_categories': ['operations', 'logistics', 'finance', 'analytics'],
        'weighted_jobs': None,
        'enabled': False,
    },
}

def get_city_config(city_key):
    """Get city configuration, returning default NY config for unknown cities."""
    return CITY_CONFIG.get(city_key, CITY_CONFIG['default'])

def get_city_job_multiplier(city_key):
    """Return the job pay multiplier for a city (1.0 = unchanged)."""
    cfg = get_city_config(city_key)
    return cfg.get('job_pay_multiplier', 1.0)

def get_city_rental_multiplier(city_key):
    """Return the rental price multiplier for a city (1.0 = unchanged)."""
    cfg = get_city_config(city_key)
    return cfg.get('rental_price_multiplier', 1.0)

def get_city_weighted_jobs(city_key):
    """Return the weighted job pool for a city, or None if not specialized."""
    cfg = get_city_config(city_key)
    if not cfg.get('enabled', True):
        return None
    return cfg.get('weighted_jobs')
