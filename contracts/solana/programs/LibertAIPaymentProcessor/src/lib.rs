use anchor_lang::prelude::*;
use anchor_spl::token::{self, Token, TokenAccount, Transfer};

declare_id!("AnAYnLu4gaHK6usSXybni24154Qg4DQuLUvkyPCJMvXu");

pub const ACCEPTED_MINT: Pubkey = pubkey!("HrGxyLboQpUAxQTDm5AKQ2vfEASo1FNnRFY3AqEi3iDk");


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
        let cpi_accounts = Transfer {
            from: ctx.accounts.user_token_account.to_account_info(),
            to: ctx.accounts.program_token_account.to_account_info(),
            authority: ctx.accounts.user.to_account_info(),
        };

        let cpi_program = ctx.accounts.token_program.to_account_info();
        let cpi_ctx = CpiContext::new(cpi_program, cpi_accounts);

        token::transfer(cpi_ctx, amount)?;

        emit!(PaymentEvent {
            user: ctx.accounts.user.key(),
            amount,
            timestamp: Clock::get()?.unix_timestamp,
            token_mint: ctx.accounts.token_mint.key(),
        });
    
        msg!("Payment processed: {} tokens from {}", amount, ctx.accounts.user.key());
        
        Ok(())
    }
    
    pub fn create_program_token_account(ctx: Context<CreateProgramTokenAccount>) -> Result<()> {
        msg!("Program token account created for mint: {}", ctx.accounts.token_mint.key());
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
        let program_token_account = &ctx.accounts.program_token_account;
        
        require!(
            program_token_account.amount >= amount,
            PaymentProcessorError::InsufficientFunds
        );

        let token_mint_key = ctx.accounts.token_mint.key();
        let seeds = &[
            b"program_token_account",
            token_mint_key.as_ref(),
            &[ctx.bumps.program_token_account],
        ];
        let signer = &[&seeds[..]];

        let cpi_accounts = Transfer {
            from: ctx.accounts.program_token_account.to_account_info(),
            to: ctx.accounts.destination_token_account.to_account_info(),
            authority: ctx.accounts.program_token_account.to_account_info(),
        };

        let cpi_program = ctx.accounts.token_program.to_account_info();
        let cpi_ctx = CpiContext::new_with_signer(cpi_program, cpi_accounts, signer);

        token::transfer(cpi_ctx, amount)?;

        msg!("Withdrawal processed: {} tokens by {} to {}", 
             amount, 
             ctx.accounts.authority.key(), 
             ctx.accounts.destination_token_account.owner);
        
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
    
    #[account(
        mut,
        constraint = user_token_account.owner == user.key(),
        constraint = user_token_account.mint == token_mint.key()
    )]
    pub user_token_account: Account<'info, TokenAccount>,
    
    #[account(
        mut,
        seeds = [b"program_token_account", token_mint.key().as_ref()],
        bump,
        constraint = program_token_account.mint == token_mint.key()
    )]
    pub program_token_account: Account<'info, TokenAccount>,
    #[account(
        constraint = token_mint.key() == ACCEPTED_MINT @ PaymentProcessorError::InvalidTokenMint
    )]
    pub token_mint: Account<'info, token::Mint>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
pub struct CreateProgramTokenAccount<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    
    #[account(
        init,
        payer = payer,
        seeds = [b"program_token_account", token_mint.key().as_ref()],
        bump,
        token::mint = token_mint,
        token::authority = program_token_account,
    )]
    pub program_token_account: Account<'info, TokenAccount>,
    
    pub token_mint: Account<'info, token::Mint>,
    pub token_program: Program<'info, Token>,
    pub system_program: Program<'info, System>,
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
        bump,
        constraint = program_token_account.mint == token_mint.key()
    )]
    pub program_token_account: Account<'info, TokenAccount>,
    
    #[account(
        mut,
        constraint = destination_token_account.mint == token_mint.key()
    )]
    pub destination_token_account: Account<'info, TokenAccount>,
    
    pub token_mint: Account<'info, token::Mint>,
    pub token_program: Program<'info, Token>,
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
    
    #[msg("Invalid token mint - only HrGxyLboQpUAxQTDm5AKQ2vfEASo1FNnRFY3AqEi3iDk is accepted")]
    InvalidTokenMint,
}
