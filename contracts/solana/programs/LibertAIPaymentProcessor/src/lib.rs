use anchor_lang::prelude::*;

declare_id!("AnAYnLu4gaHK6usSXybni24154Qg4DQuLUvkyPCJMvXu");

pub const ACCEPTED_MINT: Pubkey = pubkey!("Df3shQQ3qZ9qyLfrWTqfjP2TSSAqMvM5zxb2NXQQKaXh");

// Token program IDs
pub const SPL_TOKEN_PROGRAM_ID: Pubkey = pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
pub const TOKEN_2022_PROGRAM_ID: Pubkey = pubkey!("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");

fn is_valid_token_program(program_id: &Pubkey) -> bool {
    *program_id == SPL_TOKEN_PROGRAM_ID || *program_id == TOKEN_2022_PROGRAM_ID
}

#[program]
pub mod libert_ai_payment_processor {
    use super::*;

    pub fn initialize(ctx: Context<Initialize>, owner: Pubkey) -> Result<()> {
        let program_state = &mut ctx.accounts.program_state;

        program_state.owner = owner;
        program_state.admins = Vec::new();
        program_state.bump = ctx.bumps.program_state;

        msg!("Payment processor initialized with owner: {}", owner);
        Ok(())
    }

    pub fn process_payment(ctx: Context<ProcessPayment>, amount: u64) -> Result<()> {
        // Validate that the token program is either SPL Token or Token 2022
        require!(
            is_valid_token_program(&ctx.accounts.token_program.key()),
            PaymentProcessorError::InvalidTokenProgram
        );

        // Validate user token account manually since it can be from either token program
        require!(
            ctx.accounts.user_token_account.owner == &ctx.accounts.token_program.key(),
            PaymentProcessorError::InvalidTokenProgram
        );

        // Parse the token account data to validate mint and owner
        {
            let user_token_account_data = ctx.accounts.user_token_account.try_borrow_data()?;
            require!(
                user_token_account_data.len() >= 72, // Minimum size for both SPL Token and Token 2022 accounts
                PaymentProcessorError::InvalidTokenAccount
            );

            // For both SPL Token and Token 2022, the mint is at bytes 0-32 and owner is at bytes 32-64
            let mint_bytes = &user_token_account_data[0..32];
            let owner_bytes = &user_token_account_data[32..64];
            
            let user_token_mint = Pubkey::try_from(mint_bytes).map_err(|_| PaymentProcessorError::InvalidTokenAccount)?;
            let user_token_owner = Pubkey::try_from(owner_bytes).map_err(|_| PaymentProcessorError::InvalidTokenAccount)?;

            require!(
                user_token_mint == ctx.accounts.token_mint.key(),
                PaymentProcessorError::InvalidTokenAccount
            );
            require!(
                user_token_owner == ctx.accounts.user.key(),
                PaymentProcessorError::InvalidTokenAccount
            );
        }

        // Check if program token account needs initialization
        let needs_initialization = {
            let program_token_account_data = ctx.accounts.program_token_account.try_borrow_data()?;
            program_token_account_data.len() == 0 || program_token_account_data[0] == 0
        };
        
        if needs_initialization {
            // Initialize the program token account
            let initialize_account_ix = anchor_lang::solana_program::instruction::Instruction {
                program_id: ctx.accounts.token_program.key(),
                accounts: vec![
                    anchor_lang::solana_program::instruction::AccountMeta::new(
                        ctx.accounts.program_token_account.key(),
                        false,
                    ),
                    anchor_lang::solana_program::instruction::AccountMeta::new_readonly(
                        ctx.accounts.token_mint.key(),
                        false,
                    ),
                    anchor_lang::solana_program::instruction::AccountMeta::new_readonly(
                        ctx.accounts.program_token_account.key(),
                        false,
                    ),
                    anchor_lang::solana_program::instruction::AccountMeta::new_readonly(
                        ctx.accounts.rent.key(),
                        false,
                    ),
                ],
                data: vec![1], // InitializeAccount instruction discriminator
            };
            
            anchor_lang::solana_program::program::invoke(
                &initialize_account_ix,
                &[
                    ctx.accounts.program_token_account.to_account_info(),
                    ctx.accounts.token_mint.to_account_info(),
                    ctx.accounts.program_token_account.to_account_info(),
                    ctx.accounts.rent.to_account_info(),
                    ctx.accounts.token_program.to_account_info(),
                ],
            )?;

            msg!("Program token account initialized for mint: {}", ctx.accounts.token_mint.key());
        } else {
            // Validate existing program token account
            require!(
                ctx.accounts.program_token_account.owner == &ctx.accounts.token_program.key(),
                PaymentProcessorError::InvalidTokenProgram
            );

            let program_token_account_data = ctx.accounts.program_token_account.try_borrow_data()?;
            require!(
                program_token_account_data.len() >= 72,
                PaymentProcessorError::InvalidTokenAccount
            );

            let program_token_mint = Pubkey::try_from(&program_token_account_data[0..32])
                .map_err(|_| PaymentProcessorError::InvalidTokenAccount)?;
            
            require!(
                program_token_mint == ctx.accounts.token_mint.key(),
                PaymentProcessorError::InvalidTokenAccount
            );
        }

        // Create manual transfer instruction for Token-2022 compatibility
        let transfer_ix = anchor_lang::solana_program::instruction::Instruction {
            program_id: ctx.accounts.token_program.key(),
            accounts: vec![
                anchor_lang::solana_program::instruction::AccountMeta::new(
                    ctx.accounts.user_token_account.key(),
                    false,
                ),
                anchor_lang::solana_program::instruction::AccountMeta::new(
                    ctx.accounts.program_token_account.key(),
                    false,
                ),
                anchor_lang::solana_program::instruction::AccountMeta::new_readonly(
                    ctx.accounts.user.key(),
                    true,
                ),
            ],
            data: {
                let mut data = vec![3]; // Transfer instruction discriminator
                data.extend_from_slice(&amount.to_le_bytes());
                data
            },
        };

        anchor_lang::solana_program::program::invoke(
            &transfer_ix,
            &[
                ctx.accounts.user_token_account.to_account_info(),
                ctx.accounts.program_token_account.to_account_info(),
                ctx.accounts.user.to_account_info(),
                ctx.accounts.token_program.to_account_info(),
            ],
        )?;

        emit!(PaymentEvent {
            user: ctx.accounts.user.key(),
            amount,
            timestamp: Clock::get()?.unix_timestamp,
            token_mint: ctx.accounts.token_mint.key(),
        });
    
        msg!("Payment processed: {} tokens from {}", amount, ctx.accounts.user.key());
        
        Ok(())
    }
    

    pub fn add_admin(ctx: Context<AddAdmin>, new_admin: Pubkey) -> Result<()> {
        let program_state = &mut ctx.accounts.program_state;

        require!(
            !program_state.admins.contains(&new_admin),
            PaymentProcessorError::AdminAlreadyExists
        );

        program_state.admins.push(new_admin);
        
        msg!("Admin added: {}", new_admin);
        Ok(())
    }
    
    pub fn remove_admin(ctx: Context<RemoveAdmin>, admin_to_remove: Pubkey) -> Result<()> {
        let program_state = &mut ctx.accounts.program_state;
        let admin_position = program_state.admins.iter().position(|&x| x == admin_to_remove);

        require!(
            admin_position.is_some(),
            PaymentProcessorError::AdminNotFound
        );

        program_state.admins.remove(admin_position.unwrap());
        
        msg!("Admin removed: {}", admin_to_remove);
        Ok(())
    }

    pub fn change_owner(ctx: Context<ChangeOwner>, new_owner: Pubkey) -> Result<()> {
        let program_state = &mut ctx.accounts.program_state;
        let old_owner = program_state.owner;
        
        program_state.owner = new_owner;
        
        msg!("Owner changed from {} to {}", old_owner, new_owner);
        Ok(())
    }

    pub fn get_admins(ctx: Context<GetAdmins>) -> Result<Vec<Pubkey>> {
        let program_state = &ctx.accounts.program_state;
        Ok(program_state.admins.clone())
    }

    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        // Validate that the token program is either SPL Token or Token 2022
        require!(
            is_valid_token_program(&ctx.accounts.token_program.key()),
            PaymentProcessorError::InvalidTokenProgram
        );

        // Validate program token account manually
        require!(
            ctx.accounts.program_token_account.owner == &ctx.accounts.token_program.key(),
            PaymentProcessorError::InvalidTokenProgram
        );

        {
            let program_token_account_data = ctx.accounts.program_token_account.try_borrow_data()?;
            require!(
                program_token_account_data.len() >= 72,
                PaymentProcessorError::InvalidTokenAccount
            );

            // Parse token account data to get amount (at bytes 64-72)
            let amount_bytes = &program_token_account_data[64..72];
            let program_token_amount = u64::from_le_bytes(
                amount_bytes.try_into().map_err(|_| PaymentProcessorError::InvalidTokenAccount)?
            );
            
            require!(
                program_token_amount >= amount,
                PaymentProcessorError::InsufficientFunds
            );
        }

        // Validate destination token account manually
        require!(
            ctx.accounts.destination_token_account.owner == &ctx.accounts.token_program.key(),
            PaymentProcessorError::InvalidTokenProgram
        );

        {
            let destination_token_account_data = ctx.accounts.destination_token_account.try_borrow_data()?;
            require!(
                destination_token_account_data.len() >= 72,
                PaymentProcessorError::InvalidTokenAccount
            );

            let destination_token_mint = Pubkey::try_from(&destination_token_account_data[0..32])
                .map_err(|_| PaymentProcessorError::InvalidTokenAccount)?;
            
            require!(
                destination_token_mint == ctx.accounts.token_mint.key(),
                PaymentProcessorError::InvalidTokenAccount
            );
        }

        let token_mint_key = ctx.accounts.token_mint.key();
        let seeds = &[
            b"program_token_account",
            token_mint_key.as_ref(),
            &[ctx.bumps.program_token_account],
        ];
        let signer = &[&seeds[..]];

        // Create manual transfer instruction for Token-2022 compatibility
        let transfer_ix = anchor_lang::solana_program::instruction::Instruction {
            program_id: ctx.accounts.token_program.key(),
            accounts: vec![
                anchor_lang::solana_program::instruction::AccountMeta::new(
                    ctx.accounts.program_token_account.key(),
                    false,
                ),
                anchor_lang::solana_program::instruction::AccountMeta::new(
                    ctx.accounts.destination_token_account.key(),
                    false,
                ),
                anchor_lang::solana_program::instruction::AccountMeta::new_readonly(
                    ctx.accounts.program_token_account.key(),
                    true,
                ),
            ],
            data: {
                let mut data = vec![3]; // Transfer instruction discriminator
                data.extend_from_slice(&amount.to_le_bytes());
                data
            },
        };

        anchor_lang::solana_program::program::invoke_signed(
            &transfer_ix,
            &[
                ctx.accounts.program_token_account.to_account_info(),
                ctx.accounts.destination_token_account.to_account_info(),
                ctx.accounts.program_token_account.to_account_info(),
                ctx.accounts.token_program.to_account_info(),
            ],
            signer,
        )?;

        msg!("Withdrawal processed: {} tokens by {} to {}", 
             amount, 
             ctx.accounts.authority.key(), 
             ctx.accounts.destination_token_account.key());
        
        Ok(())
    }

    pub fn withdraw_sol(ctx: Context<WithdrawSol>, amount: u64) -> Result<()> {
        let program_state_account = &ctx.accounts.program_state;
        let rent = Rent::get()?;
        let min_balance = rent.minimum_balance(program_state_account.to_account_info().data_len());

        msg!("Program state balance: {}, Min balance needed: {}, Amount requested: {}", 
            program_state_account.to_account_info().lamports(),
            min_balance,
            amount);

        require!(
            program_state_account.to_account_info().lamports() >= amount + min_balance,
            PaymentProcessorError::InsufficientFunds
        );

        let seeds = &[
            b"program_state".as_ref(),
            &[program_state_account.bump],
        ];
        let _signer = &[&seeds[..]];

        **program_state_account.to_account_info().try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.destination.to_account_info().try_borrow_mut_lamports()? += amount;

        msg!("SOL withdrawal processed: {} lamports by {} to {}", 
             amount, 
             ctx.accounts.authority.key(), 
             ctx.accounts.destination.key());
        
        Ok(())
    }
}

#[account]
pub struct ProgramState {
    pub owner: Pubkey,
    pub admins: Vec<Pubkey>,
    pub bump: u8,
}

impl ProgramState {
    pub const INITIAL_LEN: usize = 32 + 4 + 1 + 8; // owner + vec length + bump + discriminator

    pub fn is_admin(&self, pubkey: &Pubkey) -> bool {
        self.admins.contains(pubkey)
    }
    
    pub fn is_owner_or_admin(&self, pubkey: &Pubkey) -> bool {
        self.owner == *pubkey || self.is_admin(pubkey)
    }
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(
        init,
        payer = payer,
        space = ProgramState::INITIAL_LEN,
        seeds = [b"program_state"],
        bump
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub payer: Signer<'info>,
    pub system_program: Program<'info, System>,
}


#[derive(Accounts)]
pub struct ProcessPayment<'info> {
    #[account(mut)]
    pub user: Signer<'info>,
    
    #[account(mut)]
    /// CHECK: Token account can be from either SPL Token or Token 2022 program - validated manually
    pub user_token_account: AccountInfo<'info>,
    
    #[account(
        init_if_needed,
        payer = user,
        space = 165, // Token account size (both SPL Token and Token 2022)
        seeds = [b"program_token_account", token_mint.key().as_ref()],
        bump,
        owner = token_program.key(),
    )]
    /// CHECK: Token account can be from either SPL Token or Token 2022 program - validated manually  
    pub program_token_account: AccountInfo<'info>,
    #[account(
        constraint = token_mint.key() == ACCEPTED_MINT @ PaymentProcessorError::InvalidTokenMint
    )]
    /// CHECK: Token mint can be from either SPL Token or Token 2022 program
    pub token_mint: AccountInfo<'info>,
    /// CHECK: Token program can be either SPL Token or Token 2022
    pub token_program: AccountInfo<'info>,
    pub system_program: Program<'info, System>,
    pub rent: Sysvar<'info, Rent>,
}


#[derive(Accounts)]
pub struct AddAdmin<'info> {
    #[account(
        mut,
        seeds = [b"program_state"],
        bump = program_state.bump,
        constraint = program_state.is_owner_or_admin(&authority.key()) @PaymentProcessorError::UnauthorizedAccess,
        realloc = ProgramState::INITIAL_LEN + (program_state.admins.len() + 1) * 32,
        realloc::payer = authority,
        realloc::zero = false,
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct RemoveAdmin<'info> {
    #[account(
        mut,
        seeds = [b"program_state"],
        bump = program_state.bump,
        constraint = program_state.is_owner_or_admin(&authority.key()) @PaymentProcessorError::UnauthorizedAccess,
        realloc = ProgramState::INITIAL_LEN + (program_state.admins.len().saturating_sub(1)) * 32,
        realloc::payer = authority,
        realloc::zero = false,
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ChangeOwner<'info> {
    #[account(
        mut,
        seeds = [b"program_state"],
        bump = program_state.bump,
        constraint = program_state.owner == authority.key() @PaymentProcessorError::OnlyOwnerCanChangeOwner
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub authority: Signer<'info>,
}

#[derive(Accounts)]
pub struct GetAdmins<'info> {
    #[account(
        seeds = [b"program_state"],
        bump = program_state.bump
    )]
    pub program_state: Account<'info, ProgramState>,
}

#[derive(Accounts)]
pub struct Withdraw<'info> {
    #[account(
        seeds = [b"program_state"],
        bump = program_state.bump,
        constraint = program_state.is_owner_or_admin(&authority.key()) @PaymentProcessorError::UnauthorizedAccess
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub authority: Signer<'info>,
    
    #[account(
        mut,
        seeds = [b"program_token_account", token_mint.key().as_ref()],
        bump
    )]
    /// CHECK: Token account can be from either SPL Token or Token 2022 program - validated manually
    pub program_token_account: AccountInfo<'info>,
    
    #[account(mut)]
    /// CHECK: Token account can be from either SPL Token or Token 2022 program - validated manually
    pub destination_token_account: AccountInfo<'info>,
    
    /// CHECK: Token mint can be from either SPL Token or Token 2022 program
    pub token_mint: AccountInfo<'info>,
    /// CHECK: Token program can be either SPL Token or Token 2022
    pub token_program: AccountInfo<'info>,
}

#[derive(Accounts)]
pub struct WithdrawSol<'info> {
    #[account(
        mut,
        seeds = [b"program_state"],
        bump = program_state.bump,
        constraint = program_state.is_owner_or_admin(&authority.key()) @PaymentProcessorError::UnauthorizedAccess
    )]
    pub program_state: Account<'info, ProgramState>,
    
    #[account(mut)]
    pub authority: Signer<'info>,
    
    
    /// CHECK 
    #[account(mut)]
    pub destination: AccountInfo<'info>,
}

#[event]
pub struct PaymentEvent {
    pub user: Pubkey,
    pub amount: u64,
    pub timestamp: i64,
    pub token_mint: Pubkey,
}

#[error_code]
pub enum PaymentProcessorError {
    #[msg("Unauthorized access - only owner or admin can perform this action")]
    UnauthorizedAccess,
    
    #[msg("Only the owner can change the program owner")]
    OnlyOwnerCanChangeOwner,
    
    #[msg("Admin already exists")]
    AdminAlreadyExists,
    
    #[msg("Admin not found")]
    AdminNotFound,
    
    #[msg("Insufficient funds in program token account")]
    InsufficientFunds,
    
    #[msg("Invalid token mint - only Df3shQQ3qZ9qyLfrWTqfjP2TSSAqMvM5zxb2NXQQKaXh is accepted")]
    InvalidTokenMint,
    
    #[msg("Invalid token program - only SPL Token and Token 2022 programs are accepted")]
    InvalidTokenProgram,
    
    #[msg("Invalid token account - account data is malformed or constraints not met")]
    InvalidTokenAccount,
}