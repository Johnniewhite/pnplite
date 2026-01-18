def test_onboarding():
    with open("app/services/whatsapp_service.py", "r") as f:
        content = f.read()
        
    onboard_return = 'f"Thanks, {name}! Which city are you in? (PH / Lagos / Abuja)",'
    if onboard_return in content:
        # Find the return block
        start_idx = content.find(onboard_return)
        end_idx = content.find(")", start_idx) + 1
        return_block = content[start_idx:end_idx]
        print("Found return block:")
        print(return_block)
        
        # Check for empty list
        if "[]" in return_block:
            print("\n✅ SUCCESS: Empty list [] found in awaiting_name return tuple.")
        else:
            print("\n❌ FAILURE: Empty list [] NOT found in return tuple.")
    else:
        print("Could not locate onboarding return block.")

if __name__ == "__main__":
    test_onboarding()
