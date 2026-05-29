-- Migration: Add Food-Specific Features to Mama's Kitchen
-- Date: 2026-05-30

BEGIN;

-- Add food-specific columns to products table
ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    food_category VARCHAR(100);  -- e.g., "Main Dishes", "Desserts", "Beverages", "Sides"

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    cuisine_type VARCHAR(100);   -- e.g., "Filipino", "Italian", "Asian", "Mexican"

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    preparation_time INT;        -- minutes

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    servings INT;                -- number of servings

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    ingredients TEXT;            -- comma-separated or JSON

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    allergens TEXT;              -- comma-separated: peanuts, shellfish, dairy, gluten, etc.

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    is_spicy BOOLEAN DEFAULT FALSE;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    spice_level INT;             -- 1-5 scale

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    dietary_options TEXT;        -- vegetarian, vegan, keto, etc. (comma-separated)

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    is_available_today BOOLEAN DEFAULT TRUE;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    expiration_date DATE;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    storage_instructions TEXT;   -- How to store

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    reheating_instructions TEXT;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    nutritional_info TEXT;       -- JSON: calories, protein, carbs, fat, etc.

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    origin_location VARCHAR(255); -- Where the dish is from

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    is_bestseller BOOLEAN DEFAULT FALSE;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    rating_count INT DEFAULT 0;

ALTER TABLE products ADD COLUMN IF NOT EXISTS 
    average_rating NUMERIC(3, 2) DEFAULT 0;

-- Add food-specific columns to product_variants
ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS 
    portion_size VARCHAR(100);   -- "Small", "Medium", "Large", "1 Serving", etc.

ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS 
    packaging_type VARCHAR(100); -- "Take-out Box", "Aluminum Tray", "Glass Container", etc.

ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS 
    additional_cost_reason TEXT; -- Why the price differs

-- Create table for food allergen management
CREATE TABLE IF NOT EXISTS food_allergens (
    id SERIAL PRIMARY KEY,
    product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    allergen_name VARCHAR(100) NOT NULL,  -- peanuts, tree_nuts, milk, eggs, shellfish, fish, wheat, soy, sesame
    severity VARCHAR(50),                  -- mild, moderate, severe
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, allergen_name)
);

-- Create table for dish recommendations
CREATE TABLE IF NOT EXISTS dish_recommendations (
    id SERIAL PRIMARY KEY,
    product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    recommendation_type VARCHAR(100),  -- "pairs_well_with", "goes_well_with", "substitute_for"
    recommended_product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, recommended_product_id, recommendation_type)
);

-- Create table for meal plans/combos
CREATE TABLE IF NOT EXISTS meal_combos (
    id SERIAL PRIMARY KEY,
    seller_id INT REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    combo_price NUMERIC(12, 2),
    image TEXT,
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create table for combo items
CREATE TABLE IF NOT EXISTS combo_items (
    id SERIAL PRIMARY KEY,
    combo_id INT NOT NULL REFERENCES meal_combos(id) ON DELETE CASCADE,
    product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity INT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create table for delivery time slots (for food freshness)
CREATE TABLE IF NOT EXISTS delivery_time_slots (
    id SERIAL PRIMARY KEY,
    seller_id INT REFERENCES users(id) ON DELETE CASCADE,
    day_of_week INT,           -- 0=Sunday, 6=Saturday
    start_time TIME,
    end_time TIME,
    max_orders INT,
    current_orders INT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_products_food_category ON products(food_category);
CREATE INDEX IF NOT EXISTS idx_products_cuisine_type ON products(cuisine_type);
CREATE INDEX IF NOT EXISTS idx_products_dietary_options ON products USING GIN(to_tsvector('english', dietary_options));
CREATE INDEX IF NOT EXISTS idx_products_is_bestseller ON products(is_bestseller);
CREATE INDEX IF NOT EXISTS idx_products_average_rating ON products(average_rating DESC);
CREATE INDEX IF NOT EXISTS idx_products_is_available_today ON products(is_available_today);
CREATE INDEX IF NOT EXISTS idx_food_allergens_product ON food_allergens(product_id);
CREATE INDEX IF NOT EXISTS idx_meal_combos_seller ON meal_combos(seller_id);
CREATE INDEX IF NOT EXISTS idx_delivery_slots_seller ON delivery_time_slots(seller_id);

COMMIT;
